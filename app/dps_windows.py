"""Missed DPS windows: calm stretches where the group is healthy and no big hit is coming - time a healer
can spend on damage. Each window shows what you did with it (damage casts, heals into a full group, idle) and
how much damage you dealt vs what top healers of your spec deal in their own calm moments (pulls differ
between groups, so the comparison is calm-time to calm-time, not pull to pull)."""
import statistics
from dataclasses import dataclass

from app.reports import Fight
from app.runs import RunData, _events
from app.timeline import Timeline
from app.wcl import WCLClient

CACHE_VERSION = 1
MIN_WINDOW_S = 3
CALM_EHP = 0.9  # group HP that always counts as healthy (a pull's top-group target can lower it)
NOBODY_LOW = 0.6  # every player at least this high
HIT_LOOKAHEAD_MS = 4000  # a big group hit within this is not a safe moment to start a cast
INTAKE_CALM = 0.01  # share of group max HP taken per second that still counts as no damage
GCD_S = 1.2  # rough GCD with haste, for "idle" time


@dataclass
class Window:
    start: int
    end: int
    damage: float
    dps_casts: int  # casts of spells that dealt damage
    other_casts: int  # everything else (heals into a healthy group, buffs, movement)

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / 1000

    @property
    def idle_s(self) -> float:
        return max(0.0, self.seconds - GCD_S * (self.dps_casts + self.other_casts))


def fetch_damage_done(client: WCLClient, fight: Fight, run: RunData) -> list[list]:
    """The healer's damage done: [t, amount, ability] (pets and totems excluded)."""
    key = f"dmgdone:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{run.healer_id}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    rows = [[e["timestamp"], e.get("amount", 0) + e.get("absorbed", 0), e.get("abilityGameID", 0)]
            for e in _events(client, fight, "DamageDone", source_id=run.healer_id)
            if e.get("type") == "damage" and e.get("sourceID") == run.healer_id and not e.get("targetIsFriendly")]
    client.set_cached(key, rows)
    return rows


def calm_bins(run: RunData, tl: Timeline, hits: list[int], targets: dict[int, float] | None = None) -> list[bool]:
    """Per timeline bin: in a pull, group healthy (CALM_EHP or the pull's top-group target, whichever is lower),
    nobody low, little damage coming in, and no big group hit within HIT_LOOKAHEAD_MS. targets: pull index ->
    the HP top groups hold on that pull (from the profiles); without it each pull uses its own 75th percentile."""
    n = len(tl.group_ehp)
    out = [False] * n
    for i, p in enumerate(run.pulls):
        bins = list(tl.slice(p.start, p.end))
        if not bins:
            continue
        own = sorted(tl.group_ehp[b] for b in bins)[int(len(bins) * 0.75)]
        need = min(CALM_EHP, (targets or {}).get(i, own))
        for b in bins:
            out[b] = (tl.group_ehp[b] >= need and tl.min_ehp[b] >= NOBODY_LOW
                      and tl.intake_frac[b] <= INTAKE_CALM)
    hit_bins = sorted({tl.bin_of(t) for t in hits})
    look = HIT_LOOKAHEAD_MS // tl.bin_ms
    for hb in hit_bins:  # the seconds just before a big hit are for topping, not damage
        for b in range(max(0, hb - look), hb + 1):
            out[b] = False
    return out


def find_windows(run: RunData, tl: Timeline, calm: list[bool], damage: list[list]) -> list[Window]:
    damage_ids = {d[2] for d in damage}
    out, b, n = [], 0, len(calm)
    while b < n:
        if not calm[b]:
            b += 1
            continue
        s = b
        while b < n and calm[b]:
            b += 1
        if (b - s) * tl.bin_ms / 1000 < MIN_WINDOW_S:
            continue
        t0, t1 = tl.start + s * tl.bin_ms, tl.start + b * tl.bin_ms
        casts = [a for t, a, _ in run.casts if t0 <= t < t1]
        out.append(Window(t0, t1, sum(d[1] for d in damage if t0 <= d[0] < t1),
                          sum(1 for a in casts if a in damage_ids), sum(1 for a in casts if a not in damage_ids)))
    return out


def calm_dps(windows: list[Window]) -> float | None:
    secs = sum(w.seconds for w in windows)
    return sum(w.damage for w in windows) / secs if secs >= 20 else None


def summarize(windows: list[Window], top_rate: float | None) -> dict:
    mine = calm_dps(windows)
    secs = sum(w.seconds for w in windows)
    missed = sum(max(0.0, top_rate * w.seconds - w.damage) for w in windows) if top_rate else None
    return {"seconds": round(secs), "windows": len(windows), "your_dps": round(mine) if mine else None,
            "top_dps": round(top_rate) if top_rate else None, "missed": round(missed) if missed is not None else None,
            "idle_s": round(sum(w.idle_s for w in windows)), "other_casts": sum(w.other_casts for w in windows),
            "dps_casts": sum(w.dps_casts for w in windows)}


def median_rate(rates: list[float | None]) -> float | None:
    rates = [r for r in rates if r]
    return statistics.median(rates) if rates else None
