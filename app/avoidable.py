"""Avoidable damage, learned from top logs. Untagged damage is unavoidable - that is the default;
a tag needs evidence:

- avoidable: top groups on the same pull were hit by it in at most a quarter of their runs
- extra: top groups take it too, but your group took far more ticks than they did (stood in it)
- went through: an enemy cast top groups interrupt most of the time completed in your run
"""
import statistics
from dataclasses import dataclass, field

from app.reports import Fight
from app.runs import RunData, _events
from app.segments import MELEE_ID, match_pull
from app.wcl import WCLClient, parallel_map

CACHE_VERSION = 1
MIN_TOPS = 3  # matched top pulls needed before judging an ability
AVOID_TOP_SHARE = 0.25  # "avoidable" when top groups took it in at most this share of runs
AVOID_TOP_PP = 0.3  # ...and at most this many hits per player on average
MIN_HIT_FRAC = 0.05  # ignore abilities that did less than this share of one player's max HP in total
EXTRA_RATIO, EXTRA_MIN = 3.0, 4  # "extra": 3x top groups' ticks and 4+ player-seconds above them
KICK_RATE, KICK_MIN = 0.7, 4  # interruptible: top groups kick it 70%+ of the time, seen 4+ times
WENT_THROUGH_DAMAGE_MS = 6000  # damage from the same ability this soon after a completed cast belongs to it
DEFAULT_INTERRUPT_CD_S = 15
# The log's interrupt events include stuns / CC (Leg Sweep, Polymorph...); only these count as "interrupt ready".
INTERRUPTS = {"Wind Shear", "Pummel", "Kick", "Mind Freeze", "Counterspell", "Quell", "Skull Bash", "Solar Beam",
              "Spear Hand Strike", "Rebuke", "Avenger's Shield", "Silence", "Muzzle", "Counter Shot", "Disrupt",
              "Spell Lock", "Optical Blast", "Axe Toss"}
# Stuns, knockbacks and CC that stop an enemy cast (they show up in the log as interrupts too). Only ones a player
# used in the run count as theirs (never cast = not talented).
STUNS_CC = {"Leg Sweep", "Capacitor Totem", "Shockwave", "Storm Bolt", "Intimidating Shout", "Hammer of Justice",
            "Blinding Light", "Chaos Nova", "Fel Eruption", "Sigil of Misery", "Sigil of Silence", "Sigil of Chains",
            "Mighty Bash", "Incapacitating Roar", "Typhoon", "Ursol's Vortex", "Mass Entanglement", "Shadowfury",
            "Mortal Coil", "Fear", "Howl of Terror", "Intimidation", "Binding Shot", "Psychic Scream", "Psychic Horror",
            "Thunderstorm", "Sundering", "Dragon's Breath", "Blast Wave", "Ring of Frost", "Polymorph", "Hex",
            "Gorefiend's Grasp", "Asphyxiate", "Death Grip", "Remorseless Winter", "Kidney Shot", "Cheap Shot", "Blind",
            "Gouge", "Paralysis", "Ring of Peace", "Tail Swipe", "Wing Buffet", "Landslide", "Imprison", "Repentance",
            "Holy Word: Chastise", "Shining Force", "Static Charge", "Earthquake"}
DEFAULT_CC_CD_S = 45
IGNORE = {"Stagger", "Spirit Link Totem", "Burning Rush", "Lifebloom", "Phantom Singularity", "Unstable Affliction"}


@dataclass
class Tag:
    kind: str  # avoidable / extra / went_through
    ability: str
    evidence: str
    players: dict[str, int] = field(default_factory=dict)  # name -> hits (player-seconds)

    @property
    def label(self) -> str:
        return {"avoidable": "Avoidable", "extra": "Avoidable (extra ticks)", "went_through": "Went through"}[self.kind]

    def line(self) -> str:
        return f"{self.label}: {self.ability} - {self.evidence}"


@dataclass
class Avoidance:
    by_pull: dict[int, dict[int, Tag]]  # pull index -> ability id -> tag
    went_through: list[dict]  # [{t, ability, ability_id, ready: [names], tag}]

    def tag_at(self, run: RunData, ability: int, t: int) -> Tag | None:
        """The tag for damage from `ability` at time t (avoidable on that pull, or a cast that went through)."""
        i = next((k for k, p in enumerate(run.pulls) if p.start <= t <= p.end), None)
        tag = self.by_pull.get(i, {}).get(ability) if i is not None else None
        if tag:
            return tag
        return next((w["tag"] for w in self.went_through
                     if (w["ability_id"] == ability or w["ability"] == run.ability_name(ability))
                     and 0 <= t - w["t"] <= WENT_THROUGH_DAMAGE_MS), None)

    def tag_by_name(self, run: RunData, name: str, t: int) -> Tag | None:
        """Tag for an ability known by name (death causes are names)."""
        ids = [a for a, n in run.abilities if n == name]
        return next((tag for a in ids if (tag := self.tag_at(run, a, t))), None)

    def damage_in(self, run: RunData, start: int, end: int) -> tuple[float, float, dict[str, float]]:
        """(avoidable damage, total damage, avoidable per ability name) between start and end."""
        total, avoid, names = 0.0, 0.0, {}
        for d in run.damage:
            if start <= d[0] <= end:
                amt = d[3] + d[4]
                total += amt
                tag = self.tag_at(run, d[2], d[0])
                if tag:
                    avoid += amt
                    names[tag.ability] = names.get(tag.ability, 0.0) + amt
        return avoid, total, names

    def as_dict(self) -> dict:
        return {"by_pull": {str(i): {str(a): t.__dict__ for a, t in tags.items()} for i, tags in self.by_pull.items()},
                "went_through": [{**w, "tag": w["tag"].__dict__} for w in self.went_through]}

    @staticmethod
    def from_dict(d: dict) -> "Avoidance":
        return Avoidance({int(i): {int(a): Tag(**t) for a, t in tags.items()} for i, tags in d["by_pull"].items()},
                         [{**w, "tag": Tag(**w["tag"])} for w in d["went_through"]])


def _ticks(run: RunData, pull, ignore: set[int]) -> dict[int, dict[int, int]]:
    """ability -> player -> player-seconds hit (ticks in the same second count once)."""
    out: dict[int, dict[int, set]] = {}
    for d in run.damage:
        if pull.start <= d[0] <= pull.end and d[2] != MELEE_ID and d[2] not in ignore:
            out.setdefault(d[2], {}).setdefault(d[1], set()).add(d[0] // 1000)
    return {a: {p: len(s) for p, s in per.items()} for a, per in out.items()}


def _self_effects(run: RunData) -> set[int]:
    """Abilities that only ever hit one player, on several pulls: a player's own effect (an Arcane Mage's
    Refraction, an Augmentation Evoker's Stretch Time), not a mechanic. The run data keeps no damage source."""
    targets: dict[int, set[int]] = {}
    pulls: dict[int, set[int]] = {}
    for d in run.damage:
        i = next((k for k, p in enumerate(run.pulls) if p.start <= d[0] <= p.end), None)
        if i is not None:
            targets.setdefault(d[2], set()).add(d[1])
            pulls.setdefault(d[2], set()).add(i)
    return {a for a, ts in targets.items() if len(ts) == 1 and len(pulls[a]) >= 2}


def learn_avoidable(run: RunData, tops: list[RunData], player_cast_ids: set[int]) -> dict[int, dict[int, Tag]]:
    max_hp: dict[int, float] = {}
    for d in run.damage:
        if d[6]:
            max_hp[d[1]] = max(max_hp.get(d[1], 0.0), d[6])
    hp = statistics.median(max_hp.values()) if max_hp else 1.0
    n_players = max(1, len(run.players))
    ignore = player_cast_ids | {a for a, n in run.abilities if n in IGNORE} | _self_effects(run)
    out: dict[int, dict[int, Tag]] = {}
    for i, pull in enumerate(run.pulls):
        matched = [(tr, p) for tr in tops if (p := match_pull(pull, tr))]
        if len(matched) < MIN_TOPS:
            continue
        mine = _ticks(run, pull, ignore)
        amount: dict[int, float] = {}
        for d in run.damage:
            if pull.start <= d[0] <= pull.end:
                amount[d[2]] = amount.get(d[2], 0.0) + d[3] + d[4]
        theirs = [(_ticks(tr, p, set()), (p.end - p.start) / 60000) for tr, p in matched]
        minutes = max(0.1, (pull.end - pull.start) / 60000)
        tags = {}
        for a, per in mine.items():
            if amount.get(a, 0.0) < MIN_HIT_FRAC * hp:
                continue
            total = sum(per.values())
            counts = [sum(t.get(a, {}).values()) for t, _ in theirs]
            hit_runs = sum(1 for c in counts if c)
            who = {run.player_name(p): n for p, n in sorted(per.items(), key=lambda x: -x[1])}
            mine_pp = total / n_players
            # The ability must exist in the top logs too (the enemy used it there and nobody got hit) -
            # this also drops your group's own effects, e.g. an Augmentation Evoker's Stretch Time.
            known = sum(1 for tr, _ in matched if any(x[0] == a for x in tr.abilities))
            if known < MIN_TOPS:
                continue
            top_pp = statistics.mean(counts) / n_players
            # Rarely hit, and lightly when it happens (a mechanic some top groups tank on purpose stays untagged).
            if hit_runs <= AVOID_TOP_SHARE * len(counts) and top_pp <= AVOID_TOP_PP:
                tags[a] = Tag("avoidable", run.ability_name(a),
                              f"peer groups: hit in {hit_runs} of {len(counts)} runs ({top_pp:.1f} hits/player) · "
                              f"your group: {mine_pp:.1f} hits/player", who)
                continue
            if hit_runs <= AVOID_TOP_SHARE * len(counts):
                continue  # rare in top groups but heavy when it happens: not confident either way
            rates = [c / max(0.1, m) for c, (_, m) in zip(counts, theirs)]
            expected = statistics.median(rates) * minutes
            if total >= EXTRA_RATIO * expected and total - expected >= EXTRA_MIN and max(per.values()) >= 3:
                tags[a] = Tag("extra", run.ability_name(a),
                              f"peer groups: {expected / n_players:.1f} hits/player for a pull this long · "
                              f"your group: {mine_pp:.1f}", who)
        if tags:
            out[i] = tags
    return out


# ---------- interrupts ----------
def fetch_kicks(client: WCLClient, fight: Fight, player_ids: set[int]) -> dict:
    """Completed enemy casts [t, ability, source] and interrupts [t, player, interrupt spell, interrupted spell]."""
    key = f"kicks:v{CACHE_VERSION}:{fight.report_code}:{fight.id}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    casts = [[e["timestamp"], e.get("abilityGameID", 0), e.get("sourceID")]
             for e in _events(client, fight, "Casts", hostility="Enemies") if e.get("type") == "cast"]
    kicks = [[e["timestamp"], e.get("sourceID"), e.get("abilityGameID", 0), e.get("extraAbilityGameID", 0)]
             for e in _events(client, fight, "Interrupts", hostility="Friendlies") if e.get("sourceID") in player_ids]
    out = {"casts": casts, "kicks": kicks}
    client.set_cached(key, out)
    return out


def kickable(top_kicks: list[dict], name) -> dict[int, tuple[float, int, list[str]]]:
    """Enemy spells top groups interrupt most of the time: ability -> (stop rate, times seen, CC they also use)."""
    kicked: dict[int, int] = {}
    done: dict[int, int] = {}
    runs: dict[int, int] = {}
    cc: dict[int, dict[str, int]] = {}
    for k in top_kicks:
        seen = set()
        for _, _, iid, aid in k["kicks"]:
            kicked[aid] = kicked.get(aid, 0) + 1
            seen.add(aid)
            if name(iid) not in INTERRUPTS:
                cc.setdefault(aid, {})[name(iid)] = cc.get(aid, {}).get(name(iid), 0) + 1
        for _, aid, _ in k["casts"]:
            done[aid] = done.get(aid, 0) + 1
        for aid in seen:
            runs[aid] = runs.get(aid, 0) + 1
    out = {}
    for aid, n in kicked.items():
        seen = n + done.get(aid, 0)
        if n >= KICK_MIN and runs.get(aid, 0) >= MIN_TOPS and n / seen >= KICK_RATE:
            top_cc = sorted(cc.get(aid, {}).items(), key=lambda x: -x[1])
            out[aid] = (n / seen, seen, [c for c, k in top_cc if k >= 2][:3])
    return out


def build(client: WCLClient, fight: Fight, run: RunData, tops: list[tuple[Fight, RunData]],
          group_casts: list[list]) -> Avoidance:
    """Learn and cache this run's tags (the vs-top-player comparison reads the cached copy)."""
    tags = learn_avoidable(run, [o for _, o in tops], {r[2] for r in group_casts})
    mine = fetch_kicks(client, fight, run.player_ids)
    pub = client.public()  # public top logs
    spells = kickable(parallel_map(lambda fo: fetch_kicks(pub, fo[0], fo[1].player_ids), tops), run.ability_name)
    av = Avoidance(tags, went_through(run, mine, spells, group_casts))
    client.set_cached(cache_key(fight.report_code, fight.id), av.as_dict())
    return av


def cache_key(code: str, fight_id: int) -> str:
    return f"avoidance:v{CACHE_VERSION}:{code}:{fight_id}"


def cached(client: WCLClient, code: str, fight_id: int) -> Avoidance | None:
    d = client.get_cached(cache_key(code, fight_id))
    return Avoidance.from_dict(d) if d else None


def went_through(run: RunData, mine: dict, kickable_spells: dict, group_casts: list[list]) -> list[dict]:
    """Kickable enemy casts that completed in your run, with the interrupts that were ready then
    (players who used that interrupt at least once; cooldown learned from their shortest gap)."""
    uses: dict[tuple[int, int], list[int]] = {}
    for t, src, aid, _ in group_casts:
        if run.ability_name(aid) in INTERRUPTS | STUNS_CC:
            uses.setdefault((src, aid), []).append(t)
    cd_ms: dict[int, int] = {}
    for (src, aid), ts in uses.items():
        ts.sort()
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a >= 8000]
        if gaps:
            cd_ms[aid] = min(cd_ms.get(aid, 10 ** 9), min(gaps))
    out = []
    for t, aid, _ in mine["casts"]:
        if aid not in kickable_spells:
            continue
        rate, seen, cc = kickable_spells[aid]
        def ready_at(kinds, default_s):
            out_r: dict[str, list[str]] = {}
            for (src, iid), ts in uses.items():
                if run.ability_name(iid) in kinds and not any(0 <= t - x < cd_ms.get(iid, default_s * 1000) for x in ts):
                    out_r.setdefault(run.player_name(src), []).append(run.ability_name(iid))
            return out_r
        ready_kicks, ready_cc = ready_at(INTERRUPTS, DEFAULT_INTERRUPT_CD_S), ready_at(STUNS_CC, DEFAULT_CC_CD_S)
        ready = sorted(ready_kicks)
        ev = f"stopped in {rate:.0%} of top casts ({seen} seen)"
        ev += f" · {len(ready)} interrupt{'s' if len(ready) != 1 else ''} ready: {', '.join(ready)}" if ready else \
            " · no interrupt was ready"
        if cc:
            ev += f" · peer groups also stop it with {', '.join(cc)}"
        out.append({"t": t, "ability": run.ability_name(aid), "ability_id": aid, "ready": ready, "rate": rate, "seen": seen,
                    "ready_kicks": ready_kicks, "ready_cc": ready_cc,
                    "tag": Tag("went_through", run.ability_name(aid), ev)})
    return out
