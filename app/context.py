"""Labels every second of a run (calm / pressure / spike / lethal) against that pull's
profile, finds emergency windows, detects team defensives and tracks healer cooldowns."""
from dataclasses import dataclass

from app.charges import infer_charges, learn_recharge_ms, uses, wait_ms
from app.runs import RunData
from app.segments import Profile, SegmentMetrics
from app.timeline import Timeline

LETHAL_EHP = 0.20  # any player below this = near death
SPIKE_DROP = 0.20  # group HP drop within SPIKE_DROP_BINS
SPIKE_DROP_BINS = 3
PRESSURE_MARGIN = 0.05
DEFENSIVE_MARGIN = 0.15  # mitigation this far above the pull's norm = defensives up
WINDOW_GAP_BINS = 2
GROUP_LOW_EHP, GROUP_LOW_COUNT = 0.5, 2  # group-wide danger: 2+ players under 50% at once
FALLBACK_TARGET, FALLBACK_FLOOR = 0.85, 0.65  # only when no top log matched the pull

IDLE, CALM, PRESSURE, SPIKE, LETHAL = "idle", "calm", "pressure", "spike", "lethal"
EMERGENCY = (SPIKE, LETHAL)


@dataclass
class Window:
    start: int  # first bin
    end: int  # last bin
    pull_idx: int
    kind: str  # spike or lethal
    min_ehp: float
    lowest_player: int
    deaths: list[list]
    defensive_share: float  # share of bins with team defensives up
    group_danger: bool  # 2+ players low, or a non-tank near death - not just a tank dip
    sustained_group_s: int  # seconds with 2+ players under 50% - a dip healed within a GCD or two is 0-2

    @property
    def duration_s(self) -> int:
        return self.end - self.start + 1


@dataclass
class Context:
    labels: list[str]
    pull_of_bin: list[int | None]
    defensive: list[bool]
    windows: list[Window]


def build_context(run: RunData, tl: Timeline, segs: list[SegmentMetrics],
                  profiles: list[Profile | None]) -> Context:
    n = len(tl.group_ehp)
    labels, pull_of, defensive = [IDLE] * n, [None] * n, [False] * n
    death_bins: dict[int, list[list]] = {}
    for d in run.deaths:
        death_bins.setdefault(tl.bin_of(d[0]), []).append(d)

    for i, (seg, prof) in enumerate(zip(segs, profiles)):
        target = prof.target_ehp if prof else FALLBACK_TARGET
        floor = prof.floor_ehp if prof else FALLBACK_FLOOR
        pressure = set(seg.pressure_bins)
        base_mitigation = tl.mitigation(seg.bins)
        for b in seg.bins:
            pull_of[b] = i
            g = tl.group_ehp[b]
            drop = tl.group_ehp[max(seg.bins.start, b - SPIKE_DROP_BINS)] - g
            if b in death_bins or tl.min_ehp[b] < LETHAL_EHP:
                labels[b] = LETHAL
            elif g < floor or drop >= SPIKE_DROP:
                labels[b] = SPIKE
            elif b in pressure or g < target - PRESSURE_MARGIN:
                labels[b] = PRESSURE
            else:
                labels[b] = CALM
            if base_mitigation is not None:
                m = tl.mitigation(range(max(0, b - 1), min(n, b + 2)))
                defensive[b] = m is not None and m >= base_mitigation + DEFENSIVE_MARGIN

    return Context(labels, pull_of, defensive, _windows(run, labels, pull_of, defensive, tl, death_bins))


def low_count(tl: Timeline, b: int) -> int:
    """Living players under GROUP_LOW_EHP in a bin."""
    return sum(1 for s in tl.player_ehp.values() if 0 < s[b] < GROUP_LOW_EHP)


def _group_danger(run: RunData, tl: Timeline, bins: list[int]) -> bool:
    tanks = run.tank_ids
    for b in bins:
        alive = {pid: s[b] for pid, s in tl.player_ehp.items() if s[b] > 0}
        if sum(1 for v in alive.values() if v < GROUP_LOW_EHP) >= GROUP_LOW_COUNT:
            return True
        if any(v < LETHAL_EHP for pid, v in alive.items() if pid not in tanks):
            return True
    return False


def _windows(run: RunData, labels, pull_of, defensive, tl: Timeline, death_bins) -> list[Window]:
    windows: list[Window] = []
    cur: list[int] = []

    def close():
        if not cur:
            return
        lowest = min(cur, key=lambda b: tl.min_ehp[b])
        deaths = [d for b in range(cur[0], cur[-1] + WINDOW_GAP_BINS + 1) for d in death_bins.get(b, [])]
        windows.append(Window(
            start=cur[0],
            end=cur[-1],
            pull_idx=pull_of[cur[0]],
            kind=LETHAL if any(labels[b] == LETHAL for b in cur) else SPIKE,
            min_ehp=tl.min_ehp[lowest],
            lowest_player=tl.lowest_player[lowest],
            deaths=deaths,
            defensive_share=sum(defensive[b] for b in cur) / len(cur),
            group_danger=_group_danger(run, tl, cur),
            sustained_group_s=sum(1 for b in cur if low_count(tl, b) >= GROUP_LOW_COUNT),
        ))
        cur.clear()

    for b, lab in enumerate(labels):
        if lab in EMERGENCY:
            if cur and (b - cur[-1] > WINDOW_GAP_BINS or pull_of[b] != pull_of[cur[-1]]):
                close()
            cur.append(b)
    close()
    return windows


class CooldownTracker:
    """When each healing cooldown was cast and whether it was ready at a given time.

    Effective cooldown = shortest observed gap if shorter than base (talents/CDR).
    Cooldowns never cast in the run are treated as not talented."""

    def __init__(self, run: RunData, cooldowns: dict[int, tuple[str, int]]):
        """cooldowns: ability id -> (name, base cooldown s), e.g. spec.healing_cooldowns."""
        self.cooldowns = cooldowns
        self.casts = {aid: uses([c[0] for c in run.casts if c[1] == aid]) for aid in self.cooldowns}  # channel ticks = 1 use
        self.casts = {aid: ts for aid, ts in self.casts.items() if ts}
        self.cd_ms, self.charges = {}, {}
        for aid, ts in self.casts.items():
            self.charges[aid] = infer_charges(ts, self.cooldowns[aid][1] * 1000)
            self.cd_ms[aid] = learn_recharge_ms(ts, self.cooldowns[aid][1], self.charges[aid])

    def name(self, aid: int) -> str:
        return self.cooldowns[aid][0]

    def ready(self, aid: int, t: int) -> bool:
        """A charge is free at t (a cooldown with two charges is still ready after one use)."""
        return not wait_ms(self.casts[aid], t, self.cd_ms[aid], self.charges[aid])

    def free_at(self, aid: int, t: int) -> int:
        """When a charge is free at or after t, counting the casts up to t."""
        return int(t + wait_ms(self.casts[aid], t, self.cd_ms[aid], self.charges[aid]))

    def last_before(self, aid: int, t: int) -> int | None:
        return max((c for c in self.casts[aid] if c <= t), default=None)

    def next_after(self, aid: int, t: int) -> int | None:
        return min((c for c in self.casts[aid] if c > t), default=None)

    def used_between(self, aid: int, a: int, b: int) -> bool:
        return any(a <= c <= b for c in self.casts[aid])
