"""Boss phases, learned from boss casts. M+ logs carry no phase markers, but phase changes
show up as one-off boss casts (e.g. Lightwarden Ruia's Shapeshift: Bear / Haranir) that
land well into the fight in most runs. Those become the phase boundaries.
"""
import statistics
from dataclasses import dataclass, replace

from app.reports import Fight
from app.runs import Pull, RunData, _events
from app.specs.base import Spec
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


def phase_breakdown(run: RunData, tl, casts: dict[int, list[list]],
                    tops: list[tuple[RunData, object, dict[int, list[list]]]],
                    spec: Spec) -> tuple[dict[int, list[Phase]], list[dict]]:
    """Phases of each boss pull in your run, and per-phase group HP / damage vs the same
    phase in the top logs. Returns ({pull index: phases}, rows)."""
    my_phases, rows = {}, []
    for idx, pull in enumerate(run.pulls):
        if not pull.is_boss or pull.id not in casts:
            continue
        top_pulls = []
        for trun, ttl, tcasts in tops:
            tp = next((p for p in trun.pulls if p.encounter_id == pull.encounter_id), None)
            if tp and tp.id in tcasts:
                top_pulls.append((trun, ttl, tp, tcasts[tp.id]))
        markers = learn_markers([(pull, casts[pull.id])] + [(tp, tc) for _, _, tp, tc in top_pulls])
        mine = phases_for(pull, casts[pull.id], markers)
        if len(mine) < 2:
            continue
        my_phases[idx] = mine
        top_phases = [(trun, ttl, phases_for(tp, tc, markers)) for trun, ttl, tp, tc in top_pulls]
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


def phases_for(pull: Pull, casts: list[list], markers: list[str]) -> list[Phase]:
    starts = sorted((min(t for t, n in casts if n == m) + pull.start, m)
                    for m in markers if any(n == m for _, n in casts))
    if not starts:
        return []
    bounds = [(pull.start, None)] + starts
    phases = []
    for i, (start, marker) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else pull.end
        phases.append(Phase(f"P{i + 1}" + (f" ({marker})" if marker else ""), start, end))
    return phases
