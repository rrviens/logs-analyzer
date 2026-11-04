"""Facts about enemy spells that damage numbers can't show, from the game's own spell tables (Blizzard client DB2
files as published by wago.tools) instead of hand-kept notes: dispel type (magic, curse, disease, poison), whether a
cast can be interrupted, cast time, and whether the damage lands on the ground (a point, a puddle, a frontal cone) -
the kind you dodge rather than survive.

Refreshed in the background about once a week into the cache database, so new dungeons and patches come in on
their own. Until the first download finishes every lookup answers "unknown" and callers fall back to what they
learn from logs.
"""
import csv
import logging
import sqlite3
import threading
import time

import httpx

from app.config import CACHE_PATH

WAGO = "https://wago.tools/db2/{}/csv"
REFRESH_S = 7 * 86400
DISPEL_NAMES = {1: "magic", 2: "curse", 3: "disease", 4: "poison"}
INTERRUPT_FLAG = 0x08  # SpellInterrupts.InterruptFlags: the cast can be interrupted
# SpellEffect.ImplicitTarget values (the game's targeting rules) that put damage on a spot on the ground rather than
# on a chosen player or the whole room around the caster: a picked point, random points, a missile's landing spot,
# a dynamic object (puddle), a circle dropped on a player. Checked against Hailbombs (87) vs Inferno, Hailburst,
# Oozing Xylem, Spouting Floret, Bedrock Surge, Thornblade (caster- or player-centered).
GROUND_TARGETS = {28, 53, 63, 72, 73, 86, 87, 88, 89}
CONE_TARGETS = {24, 54, 104, 110}  # frontal cones: dodgeable for everyone but the tank
PERSISTENT_AREA = 27  # SpellEffect.Effect: a lasting area (puddle) on the ground
TRIGGER_EFFECTS = {64, 142}  # effects that cast another spell - which inherits where the first one landed
# SpellMechanic values that take control away (charm, disorient, fear, sleep, stun, freeze, incapacitate, polymorph,
# banish, shackle, turn, horror, sap): the player can't act, so can't dispel or defend themselves.
LOSS_OF_CONTROL = {1: "charm", 2: "disorient", 5: "fear", 10: "sleep", 12: "stun", 13: "freeze", 14: "incapacitate",
                   17: "polymorph", 18: "banish", 20: "shackle", 23: "turn", 24: "horror", 30: "sap"}
log = logging.getLogger("uvicorn.error")


class SpellFacts:
    def __init__(self, db_path):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._building = False
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS spellfacts_meta (key TEXT PRIMARY KEY, value REAL)")
            cols = [r[1] for r in self._db.execute("PRAGMA table_info(spellfacts)")]
            if cols and "mechanic" not in cols:  # older layout: drop it, the next refresh rebuilds it
                self._db.execute("DROP TABLE spellfacts")
                self._db.execute("DELETE FROM spellfacts_meta")
            self._db.execute("CREATE TABLE IF NOT EXISTS spellfacts (id INTEGER PRIMARY KEY, dispel INTEGER, "
                             "interrupt INTEGER, cast_ms INTEGER, ground INTEGER, mechanic INTEGER)")
            self._db.commit()

    def _row(self, spell_id: int):
        with self._lock:
            return self._db.execute("SELECT dispel, interrupt, cast_ms, ground, mechanic FROM spellfacts WHERE id = ?",
                                    (spell_id,)).fetchone()

    def dispel(self, spell_id: int) -> str | None:
        """'magic' / 'curse' / 'disease' / 'poison', or None (not dispellable, or not known)."""
        row = self._row(spell_id)
        return DISPEL_NAMES.get(row[0]) if row else None

    def interruptible(self, spell_id: int) -> bool | None:
        """True / False from the game data; None when the spell isn't in it (yet)."""
        row = self._row(spell_id)
        return None if not row or row[1] is None else bool(row[1] & INTERRUPT_FLAG)

    def cast_ms(self, spell_id: int) -> int | None:
        """Base cast time in ms (None: instant or not known)."""
        row = self._row(spell_id)
        return row[2] if row and row[2] else None

    def ground(self, spell_id: int) -> str | None:
        """'ground' (a spot on the floor or a puddle), 'cone' (a frontal), or None."""
        row = self._row(spell_id)
        return {1: "ground", 2: "cone"}.get(row[3]) if row else None

    def loss_of_control(self, spell_id: int) -> str | None:
        """"charm" / "stun" / "fear"... when the debuff takes control away from the player, else None."""
        row = self._row(spell_id)
        return LOSS_OF_CONTROL.get(row[4]) if row else None

    def ready(self) -> bool:
        """The game data has been downloaded (until then callers keep their log-only behavior)."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM spellfacts LIMIT 1").fetchone() is not None

    def refresh_if_stale(self) -> None:
        """Starts a background download when the facts are older than a week (or missing)."""
        with self._lock:
            row = self._db.execute("SELECT value FROM spellfacts_meta WHERE key = 'built'").fetchone()
        if (row and time.time() - row[0] < REFRESH_S) or self._building:
            return
        self._building = True
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self) -> None:
        try:
            facts: dict[int, list] = {}  # id -> [dispel, interrupt flags, cast_ms, ground, mechanic]
            row = lambda sid: facts.setdefault(sid, [None, None, None, None, None])
            for r in _stream("SpellCategories"):
                d = int(r["DispelType"] or 0)
                if d in DISPEL_NAMES:
                    row(int(r["SpellID"]))[0] = d
                m = int(r["Mechanic"] or 0)
                if m in LOSS_OF_CONTROL:
                    row(int(r["SpellID"]))[4] = m
            for r in _stream("SpellInterrupts"):
                f = row(int(r["SpellID"]))
                f[1] = (f[1] or 0) | int(r["InterruptFlags"] or 0)  # any difficulty's flags
            cast_ms = {int(r["ID"]): int(r["Base"] or 0) for r in _stream("SpellCastTimes")}
            for r in _stream("SpellMisc"):
                ms = cast_ms.get(int(r["CastingTimeIndex"] or 0), 0)
                if ms:
                    row(int(r["SpellID"]))[2] = ms
            ground, triggers = {}, []  # spell -> 1 ground / 2 cone; (parent, triggered spell)
            for r in _stream("SpellEffect"):
                sid, targets = int(r["SpellID"] or 0), {int(r["ImplicitTarget_0"] or 0), int(r["ImplicitTarget_1"] or 0)}
                effect = int(r["Effect"] or 0)
                if int(r["EffectMechanic"] or 0) in LOSS_OF_CONTROL:
                    row(sid)[4] = row(sid)[4] or int(r["EffectMechanic"])
                if targets & GROUND_TARGETS or effect == PERSISTENT_AREA:
                    ground[sid] = 1
                elif targets & CONE_TARGETS:
                    ground.setdefault(sid, 2)
                if effect in TRIGGER_EFFECTS and int(r["EffectTriggerSpell"] or 0):
                    triggers.append((sid, int(r["EffectTriggerSpell"])))
            for parent, child in triggers:  # the damage in the log is often the spell the ground effect casts
                if parent in ground:
                    ground.setdefault(child, ground[parent])
            for sid, kind in ground.items():
                row(sid)[3] = kind
            with self._lock:
                self._db.execute("DELETE FROM spellfacts")
                self._db.executemany("INSERT INTO spellfacts VALUES (?, ?, ?, ?, ?, ?)",
                                     ((k, *v) for k, v in facts.items()))
                self._db.execute("INSERT OR REPLACE INTO spellfacts_meta VALUES ('built', ?)", (time.time(),))
                self._db.commit()
            log.info("spell facts: %d spells refreshed", len(facts))
        except (httpx.HTTPError, ValueError, KeyError, csv.Error) as e:
            log.warning("spell facts refresh failed (will retry next start): %s", e)
        finally:
            self._building = False


FACTS = SpellFacts(CACHE_PATH)


def _stream(table: str):
    """Rows of one DB2 table, read line by line (the files are tens of MB; nothing is held at once)."""
    with httpx.stream("GET", WAGO.format(table), timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        yield from csv.DictReader(r.iter_lines())
