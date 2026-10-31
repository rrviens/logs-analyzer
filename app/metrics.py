"""Resto Shaman spell tracking on top of RunData. Pure functions - no API calls."""
from app.runs import RunData
from app.specs import resto_shaman as rs
from app.timeline import Timeline


def riptide_active(run: RunData, tl: Timeline) -> list[int]:
    """Number of the healer's Riptides active in each timeline bin."""
    counts = [0] * len(tl.group_ehp)
    open_since: dict[int, int] = {}
    intervals: list[tuple[int, int]] = []
    for t, kind, target in sorted(run.riptide, key=lambda r: r[0]):
        if kind == "applybuff":
            open_since[target] = t
        elif kind == "refreshbuff":
            open_since.setdefault(target, t)
        elif kind == "removebuff":
            intervals.append((open_since.pop(target, run.start), t))
    intervals.extend((since, run.end) for since in open_since.values())
    for a, b in intervals:
        for i in range(tl.bin_of(a), tl.bin_of(b)):
            counts[i] += 1
    return counts


def riptide_refreshes(run: RunData) -> list[tuple[int, bool, float]]:
    """(time, early, wasted_ms) per refresh. Early = more left than the pandemic window."""
    duration_ms = rs.RIPTIDE_DURATION_S * 1000
    pandemic_ms = duration_ms * rs.PANDEMIC_FRACTION
    expiry: dict[int, float] = {}
    out = []
    for t, kind, target in sorted(run.riptide, key=lambda r: r[0]):
        if kind == "applybuff":
            expiry[target] = t + duration_ms
        elif kind == "refreshbuff":
            remaining = max(0.0, expiry.get(target, t) - t)
            early = remaining > pandemic_ms
            out.append((t, early, remaining - pandemic_ms if early else 0.0))
            expiry[target] = t + duration_ms + min(remaining, pandemic_ms)
        elif kind == "removebuff":
            expiry.pop(target, None)
    return out
