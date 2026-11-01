"""Facts about enemy spells that damage numbers can't show, from outside sources instead of hand-kept notes:

- the game's own spell tables (Blizzard client DB2 files as published by wago.tools): dispel type (magic, curse,
  disease, poison), whether a cast can be interrupted, and cast time;
- the community's avoidable-damage list (the ElitismHelper addon): spells you're meant to dodge.

Both are refreshed in the background about once a week into the cache database, so new dungeons and patches come
in on their own. Until the first download finishes every lookup answers "unknown" and callers fall back to what
they learn from logs.
"""
import csv
import logging
import re
import sqlite3
import threading
import time

import httpx

from app.config import CACHE_PATH

WAGO = "https://wago.tools/db2/{}/csv"
ELITISM = "https://raw.githubusercontent.com/amki/ElitismHelper/master/ElitismHelper.lua"
REFRESH_S = 7 * 86400
DISPEL_NAMES = {1: "magic", 2: "curse", 3: "disease", 4: "poison"}
INTERRUPT_FLAG = 0x08  # SpellInterrupts.InterruptFlags: the cast can be interrupted
log = logging.getLogger("uvicorn.error")


class SpellFacts:
    def __init__(self, db_path):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._building = False
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS spellfacts (id INTEGER PRIMARY KEY, dispel INTEGER, "
                             "interrupt INTEGER, cast_ms INTEGER, avoid INTEGER)")
            self._db.execute("CREATE TABLE IF NOT EXISTS spellfacts_meta (key TEXT PRIMARY KEY, value REAL)")
            self._db.commit()

    def _row(self, spell_id: int):
        with self._lock:
            return self._db.execute("SELECT dispel, interrupt, cast_ms, avoid FROM spellfacts WHERE id = ?",
                                    (spell_id,)).fetchone()

    def dispel(self, spell_id: int) -> str | None:
        """'magic' / 'curse' / 'disease' / 'poison', or None (not dispellable, or not known)."""
        row = self._row(spell_id)
        return DISPEL_NAMES.get(row[0]) if row else None

    def interruptible(self, spell_id: int) -> bool | None:
        """True / False from the game data; None when the spell isn't in it (yet)."""
        row = self._row(spell_id)
        return None if not row or row[1] is None else bool(row[1] & INTERRUPT_FLAG)

    def avoidable(self, spell_id: int) -> bool:
        row = self._row(spell_id)
        return bool(row and row[3])

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
            facts: dict[int, list] = {}  # id -> [dispel, interrupt flags, cast_ms, avoid]
            row = lambda sid: facts.setdefault(sid, [None, None, None, 0])
            for r in _stream("SpellCategories"):
                d = int(r["DispelType"] or 0)
                if d in DISPEL_NAMES:
                    row(int(r["SpellID"]))[0] = d
            for r in _stream("SpellInterrupts"):
                f = row(int(r["SpellID"]))
                f[1] = (f[1] or 0) | int(r["InterruptFlags"] or 0)  # any difficulty's flags
            cast_ms = {int(r["ID"]): int(r["Base"] or 0) for r in _stream("SpellCastTimes")}
            for r in _stream("SpellMisc"):
                ms = cast_ms.get(int(r["CastingTimeIndex"] or 0), 0)
                if ms:
                    row(int(r["SpellID"]))[2] = ms
            for sid in _elitism_ids():
                row(sid)[3] = 1
            with self._lock:
                self._db.execute("DELETE FROM spellfacts")
                self._db.executemany("INSERT INTO spellfacts VALUES (?, ?, ?, ?, ?)",
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


def _elitism_ids() -> set[int]:
    """Spell ids the addon counts as avoidable (commented-out lines are older seasons and don't count)."""
    text = httpx.get(ELITISM, timeout=60, follow_redirects=True).text
    return {int(m.group(1)) for line in text.splitlines()
            if not line.lstrip().startswith("--") and (m := re.match(r"\s*\[(\d+)\]\s*=\s*\d+", line))}
