"""Boss fight parts. M+ logs carry no phase markers, so parts come from two places:
- one-off boss casts that land well into the fight in most runs (Lightwarden Ruia's Shapeshift: Bear / Haranir,
  Melidrussa's Awaken Whelps);
- boss HP thresholds the Encounter Journal states ("upon reaching 40% health, Kyrakka lands") - for changes no
  cast shows. The boss's HP comes from the players' hits on it.
A part is named after what began it.
"""
import statistics
from dataclasses import dataclass, replace

from app.reports import Fight
from app.runs import Pull, RunData, _events
from app.specs.base import Spec
from app.spellfacts import FACTS
from app.wcl import WCLClient

CACHE_VERSION = 1
MARKER_MAX_CASTS = 2  # a phase marker is cast at most this often per fight
MARKER_MIN_FRACTION = 0.15  # ...and first cast at least this far into the fight
MARKER_MIN_SHARE = 0.75  # ...in this share of runs
SAME_PHASE_FRACTION = 0.08  # markers this close together start the same phase
MAX_MARKERS = 3


@dataclass
class Phase:
    name: str  # "P1", "P2 (Shapeshift: Bear)"
    start: int  # ms, report time
    end: int


def fetch_boss_casts(client: WCLClient, fight: Fight, run: RunData) -> dict[int, list[list]]:
    """Enemy casts per boss pull: {pull_id: [[ms since pull start, ability name], ...]}."""
    key = f"bosscasts:v{CACHE_VERSION}:{fight.report_code}:{fight.id}"
    cached = client.get_cached(key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    out = {}
    for p in run.pulls:
        if not p.is_boss:
            continue
        window = replace(fight, start=p.start, end=p.end)
        out[p.id] = [[e["timestamp"] - p.start, run.ability_name(e.get("abilityGameID", 0))]
                     for e in _events(client, window, "Casts", hostility="Enemies") if e.get("type") == "cast"]
    client.set_cached(key, out)
    return out


BOSS_ACTORS_QUERY = """query($code: String!) { reportData { report(code: $code) {
  masterData { actors(type: "NPC") { id gameID subType } } } } }"""
HP_CACHE_VERSION = 1
TARGET_RESOURCES = 2  # resourceActor: the HP in a damage event is the target's (the boss)


def fetch_boss_hp(client: WCLClient, fight: Fight, run: RunData) -> dict[int, list[list]]:
    """Lowest boss HP % per second of each boss pull whose fight changes at an HP threshold (the journal says so):
    {pull_id: [[ms since pull start, pct], ...]}. One cheap request per boss unit, cached."""
    key = f"bosshp:v{HP_CACHE_VERSION}:{fight.report_code}:{fight.id}"
    cached = client.get_cached(key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    pulls = [p for p in run.pulls if p.is_boss and FACTS.hp_thresholds(p.encounter_id)]
    actors = []
    if pulls:
        actors = client.query(BOSS_ACTORS_QUERY, {"code": fight.report_code})["reportData"]["report"]["masterData"]["actors"]
    out = {}
    for p in pulls:
        per_s: dict[int, float] = {}  # "either of them reaching 40%": the lowest boss unit counts
        for boss in (a["id"] for a in actors if a["subType"] == "Boss" and a["gameID"] in p.npc_ids):
            for e in _events(client, replace(fight, start=p.start, end=p.end), "DamageDone", hostility="Friendlies",
                             target_id=boss, resources=True):
                if e.get("type") == "damage" and e.get("maxHitPoints") and e.get("resourceActor") == TARGET_RESOURCES:
                    s = (e["timestamp"] - p.start) // 1000
                    per_s[s] = min(per_s.get(s, 100.0), 100.0 * e["hitPoints"] / e["maxHitPoints"])
        out[p.id] = [[s * 1000, round(v, 1)] for s, v in sorted(per_s.items())]
    client.set_cached(key, out)
    return out


def learn_markers(samples: list[tuple[Pull, list[list]]]) -> list[str]:
    """Phase-start abilities for one boss, from several runs of it."""
    if len(samples) < 2:
        return []
    stats: dict[str, list[tuple[int, float]]] = {}
    for pull, casts in samples:
        duration = max(1, pull.end - pull.start)
        per: dict[str, list[int]] = {}
        for t, name in casts:
            per.setdefault(name, []).append(t)
        for name, ts in per.items():
            stats.setdefault(name, []).append((len(ts), min(ts) / duration))
    candidates = []
    for name, rows in stats.items():
        if len(rows) / len(samples) < MARKER_MIN_SHARE:
            continue
        if max(n for n, _ in rows) > MARKER_MAX_CASTS or min(f for _, f in rows) < MARKER_MIN_FRACTION:
            continue
        candidates.append((statistics.median(f for _, f in rows), name))
    candidates.sort()
    markers, last = [], -1.0
    for frac, name in candidates:
        if frac - last >= SAME_PHASE_FRACTION:
            markers.append(name)
            last = frac
    return markers[:MAX_MARKERS]


def phase_breakdown(run: RunData, tl, casts: dict[int, list[list]], hp: dict[int, list[list]],
                    tops: list[tuple[RunData, object, dict[int, list[list]], dict[int, list[list]]]],
                    spec: Spec) -> tuple[dict[int, list[Phase]], list[dict]]:
    """Phases of each boss pull in your run, and per-phase group HP / damage vs the same
    phase in the top logs. Returns ({pull index: phases}, rows)."""
    my_phases, rows = {}, []
    for idx, pull in enumerate(run.pulls):
        if not pull.is_boss or pull.id not in casts:
            continue
        top_pulls = []
        for trun, ttl, tcasts, thp in tops:
            tp = next((p for p in trun.pulls if p.encounter_id == pull.encounter_id), None)
            if tp and tp.id in tcasts:
                top_pulls.append((trun, ttl, tp, tcasts[tp.id], thp.get(tp.id, [])))
        markers = learn_markers([(pull, casts[pull.id])] + [(tp, tc) for _, _, tp, tc, _ in top_pulls])
        limits = FACTS.hp_thresholds(pull.encounter_id)
        mine = phases_for(pull, casts[pull.id], markers, hp.get(pull.id, []), limits)
        if len(mine) < 2:
            continue
        my_phases[idx] = mine
        top_phases = [(trun, ttl, phases_for(tp, tc, markers, th, limits)) for trun, ttl, tp, tc, th in top_pulls]
        for k, ph in enumerate(mine):
            same = [(trun, ttl, tph[k]) for trun, ttl, tph in top_phases if len(tph) > k and tph[k].name == ph.name]
            top_hp = [_median_hp(ttl, p) for _, ttl, p in same]
            top_in = [_mean_intake(ttl, p) for _, ttl, p in same]
            cds = {}
            for aid, (name, _) in spec.healing_cooldowns.items():
                pressed = sum(1 for trun, _, p in same if any(a == aid and p.start <= t < p.end for t, a, _ in trun.casts))
                if pressed:
                    cds[name] = {"top": pressed, "you": any(a == aid and ph.start <= t < ph.end for t, a, _ in run.casts)}
            rows.append({
                "pull_idx": idx, "phase": ph.name, "start": ph.start, "end": ph.end,
                "duration_s": round((ph.end - ph.start) / 1000),
                "group_hp": _median_hp(tl, ph), "top_group_hp": statistics.median(top_hp) if top_hp else None,
                "intake": _mean_intake(tl, ph), "top_intake": statistics.median(top_in) if top_in else None,
                "top_n": len(same), "cooldowns": cds,
                "deaths": sum(1 for d in run.deaths if ph.start <= d[0] < ph.end),
            })
    return my_phases, rows


def _median_hp(tl, ph: Phase) -> float:
    return statistics.median(tl.group_ehp[b] for b in tl.slice(ph.start, ph.end))


def _mean_intake(tl, ph: Phase) -> float:
    bins = tl.slice(ph.start, ph.end)
    return sum(tl.intake_frac[b] for b in bins) / len(bins)


def phases_for(pull: Pull, casts: list[list], markers: list[str], hp: list[list] = (),
               limits: list[float] = ()) -> list[Phase]:
    """Parts of the fight named after what started them ("Start", "Awaken Whelps", "Awaken Whelps 2", "40% health"):
    every cast of a marker (they're cast at most twice) opens a part, and so does the boss reaching a journal HP
    threshold - unless a cast already opened a part at that moment (the cast's name says more)."""
    duration = max(1, pull.end - pull.start)
    near = SAME_PHASE_FRACTION * duration
    seen: dict[str, int] = {}
    bounds = [(pull.start, "Start")]
    for t, name in sorted((t, n) for t, n in casts if n in markers):
        if t / duration < MARKER_MIN_FRACTION or (t + pull.start) - bounds[-1][0] < near:
            continue
        seen[name] = seen.get(name, 0) + 1
        bounds.append((t + pull.start, name if seen[name] == 1 else f"{name} {seen[name]}"))
    for pct in limits:
        t = next((t for t, v in hp if v <= pct), None)
        if t is not None and all(abs(t + pull.start - b) >= near for b, _ in bounds):
            bounds.append((t + pull.start, f"{pct:g}% health"))
    bounds.sort()
    if len(bounds) < 2:
        return []
    return [Phase(label, start, bounds[i + 1][0] if i + 1 < len(bounds) else pull.end)
            for i, (start, label) in enumerate(bounds)]
