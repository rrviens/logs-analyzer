"""Per-pull metrics, matching pulls across runs, and building "what top groups do" profiles.

"Topped" is not a fixed number: each pull's profile is learned from the top logs at
the same dungeon and similar key level.
"""
import statistics
from dataclasses import dataclass, field

from app.metrics import hot_refreshes, hots_active
from app.runs import Pull, RunData
from app.specs.base import Spec
from app.timeline import Timeline

MELEE_ID = 1
GROUP_HIT_WINDOW_MS = 1500
GROUP_HIT_MIN_TARGETS = 3
GROUP_HIT_MIN_FRAC = 0.12  # avg share of max HP per target
RECOVERY_CAP_S = 30
PRESSURE_WINDOW_S = 5
TRASH_MATCH_MIN = 0.5  # Jaccard similarity of enemy NPC sets


@dataclass
class GroupHit:
    ability: int
    t: int
    targets: int
    avg_frac: float
    pre_ehp: float
    recovery_s: float | None  # back to min(pre-hit, 90%)
    min_after: float  # lowest player within 5s


@dataclass
class SegmentMetrics:
    pull: Pull
    bins: range
    duration_s: float
    median_ehp: float
    p10_ehp: float
    intake_frac_ps: float  # share of group max HP lost per second
    intake_hps: float
    healing_hps: float
    healer_hps: float
    ability_intake: dict[int, float]  # share of group max HP per second, by ability
    hits: list[GroupHit]
    pressure_bins: list[int]  # busier-than-typical moments of this pull
    hot_pressure_uptime: float | None  # maintenance HoT (Riptide) uptime in the busy moments
    hot_wasted_pressure_s: float  # duration thrown away by early refreshes in pressure
    intake_modifiers: frozenset = frozenset()  # comp effects that lower damage taken (see composition.py)


@dataclass
class Profile:
    n: int  # matched top runs
    target_ehp: float  # group HP top groups hold through this pull
    floor_ehp: float  # their 10th percentile - below this is a real dip
    intake_frac_ps: float
    intake_hps: float
    healing_hps: float
    ability_intake: dict[int, float] = field(default_factory=dict)
    hit_pre: dict[int, float] = field(default_factory=dict)
    hit_recovery: dict[int, float] = field(default_factory=dict)
    hit_count: dict[int, int] = field(default_factory=dict)  # hits observed across top runs
    hot_pressure_uptime: float | None = None
    hot_wasted_pressure_s: float = 0.0
    segments: list = field(default_factory=list)  # the matched top segments, for comp-aware subsets


def segment_metrics(run: RunData, tl: Timeline, pull: Pull, hots: list[int] | None = None,
                    refreshes: list[tuple[int, bool, float]] | None = None) -> SegmentMetrics:
    bins = tl.slice(pull.start, pull.end)
    duration_s = max(1.0, len(bins) * tl.bin_ms / 1000)
    g = [tl.group_ehp[b] for b in bins]
    group_max = _group_max_hp(run)

    ability_intake: dict[int, float] = {}
    for d in run.damage:
        if pull.start <= d[0] < pull.end:
            ability_intake[d[2]] = ability_intake.get(d[2], 0.0) + d[3] + d[4]
    ability_intake = {a: v / group_max / duration_s for a, v in ability_intake.items()}

    rolling = _rolling([tl.intake_frac[b] for b in bins], PRESSURE_WINDOW_S)
    typical = statistics.median(rolling) if rolling else 0.0
    pressure = [b for b, r in zip(bins, rolling) if r > typical and r > 0]

    rip_uptime = None
    if hots is not None and pressure:
        rip_uptime = sum(1 for b in pressure if hots[b] > 0) / len(pressure)
    pressure_set = set(pressure)
    wasted_ms = sum(w for t, early, w in refreshes or [] if early and tl.bin_of(t) in pressure_set
                    and pull.start <= t < pull.end)

    return SegmentMetrics(
        pull=pull,
        bins=bins,
        duration_s=duration_s,
        median_ehp=statistics.median(g),
        p10_ehp=_quantile(g, 0.1),
        intake_frac_ps=sum(tl.intake_frac[b] for b in bins) / len(bins),
        intake_hps=sum(tl.intake[b] for b in bins) / duration_s,
        healing_hps=sum(tl.healing[b] for b in bins) / duration_s,
        healer_hps=sum(tl.healer_healing[b] for b in bins) / duration_s,
        ability_intake=ability_intake,
        hits=group_hits(run, tl, pull),
        pressure_bins=pressure,
        hot_pressure_uptime=rip_uptime,
        hot_wasted_pressure_s=wasted_ms / 1000,
    )


def all_segments(run: RunData, tl: Timeline, spec: Spec) -> list[SegmentMetrics]:
    hots = hots_active(run, tl) if spec.maintenance else None
    refreshes = hot_refreshes(run, spec)
    return [segment_metrics(run, tl, p, hots, refreshes) for p in run.pulls]


def group_hits(run: RunData, tl: Timeline, pull: Pull) -> list[GroupHit]:
    """Enemy abilities that hit >= 3 players for a meaningful share of their HP at once."""
    max_hp = _player_max_hp(run)
    by_ability: dict[int, list[list]] = {}
    for d in run.damage:
        if pull.start <= d[0] < pull.end and d[2] != MELEE_ID:
            by_ability.setdefault(d[2], []).append(d)

    hits = []
    for ability, rows in by_ability.items():
        rows.sort(key=lambda r: r[0])
        i = 0
        while i < len(rows):
            t0 = rows[i][0]
            cluster = []
            while i < len(rows) and rows[i][0] - t0 <= GROUP_HIT_WINDOW_MS:
                cluster.append(rows[i])
                i += 1
            per_target: dict[int, float] = {}
            for r in cluster:
                per_target[r[1]] = per_target.get(r[1], 0.0) + (r[3] + r[4]) / max_hp.get(r[1], 1.0)
            if len(per_target) < GROUP_HIT_MIN_TARGETS:
                continue
            avg = sum(per_target.values()) / len(per_target)
            if avg < GROUP_HIT_MIN_FRAC:
                continue
            b0 = tl.bin_of(t0)
            pre = tl.group_ehp[max(0, b0 - 1)]
            goal = min(pre, 0.9)
            recovery = next(
                (k for k in range(1, RECOVERY_CAP_S + 1)
                 if b0 + k < len(tl.group_ehp) and tl.group_ehp[b0 + k] >= goal),
                None,
            )
            hits.append(GroupHit(
                ability=ability,
                t=t0,
                targets=len(per_target),
                avg_frac=avg,
                pre_ehp=pre,
                recovery_s=float(recovery) if recovery else None,
                min_after=min(tl.min_ehp[b0:b0 + 5] or [1.0]),
            ))
    hits.sort(key=lambda h: h.t)
    return hits


def match_pull(pull: Pull, other: RunData) -> Pull | None:
    if pull.is_boss:
        return next((p for p in other.pulls if p.encounter_id == pull.encounter_id), None)
    mine = set(pull.npc_ids)
    best, best_score = None, 0.0
    for p in other.pulls:
        if p.is_boss or not mine:
            continue
        theirs = set(p.npc_ids)
        score = len(mine & theirs) / len(mine | theirs) if theirs else 0.0
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= TRASH_MATCH_MIN else None


def build_profile(top: list[SegmentMetrics]) -> Profile | None:
    if not top:
        return None
    med = statistics.median

    abilities = {a for s in top for a in s.ability_intake}
    hit_pre: dict[int, list[float]] = {}
    hit_rec: dict[int, list[float]] = {}
    for s in top:
        for h in s.hits:
            hit_pre.setdefault(h.ability, []).append(h.pre_ehp)
            hit_rec.setdefault(h.ability, []).append(h.recovery_s if h.recovery_s is not None else RECOVERY_CAP_S)
    rip = [s.hot_pressure_uptime for s in top if s.hot_pressure_uptime is not None]

    return Profile(
        n=len(top),
        target_ehp=med(s.median_ehp for s in top),
        floor_ehp=med(s.p10_ehp for s in top),
        intake_frac_ps=med(s.intake_frac_ps for s in top),
        intake_hps=med(s.intake_hps for s in top),
        healing_hps=med(s.healing_hps for s in top),
        ability_intake={a: med(s.ability_intake.get(a, 0.0) for s in top) for a in abilities},
        hit_pre={a: med(v) for a, v in hit_pre.items()},
        hit_recovery={a: med(v) for a, v in hit_rec.items()},
        hit_count={a: len(v) for a, v in hit_pre.items()},
        hot_pressure_uptime=med(rip) if rip else None,
        hot_wasted_pressure_s=med(s.hot_wasted_pressure_s for s in top),
        segments=list(top),
    )


def _player_max_hp(run: RunData) -> dict[int, float]:
    out: dict[int, float] = {}
    for d in run.damage:
        if d[6]:
            out[d[1]] = max(out.get(d[1], 0.0), d[6])
    return out


def _group_max_hp(run: RunData) -> float:
    return sum(_player_max_hp(run).values()) or 1.0


def _rolling(values: list[float], n: int) -> list[float]:
    out, acc = [], 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= n:
            acc -= values[i - n]
        out.append(acc / min(i + 1, n))
    return out


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0
