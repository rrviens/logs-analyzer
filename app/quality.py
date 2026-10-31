"""Data-quality gate for top logs. Logs without real resource data (no Advanced Combat
Logging, partial uploads) produce flat HP lines and would flatten every baseline built
on them, so they're checked before use - on the data, never on region or name."""
import statistics

MIN_HP_SHARE = 0.4  # share of damage rows carrying HP
MIN_DISTINCT_HP = 50  # per player - constant HP means fake/missing resources
MIN_GROUP_STD = 0.03  # a group HP line that never moves
MIN_DURATION_RATIO = 0.6  # vs your run - shorter means a partial upload


def check(damage: list[list], player_ids: set[int], duration_s: float, ref_duration_s: float | None = None,
          group_ehp: list[float] | None = None) -> tuple[bool, list[str]]:
    """Damage rows: [t, target, ability, amount, absorbed, hp, max_hp, ...]. Returns (ok, reasons)."""
    reasons = []
    with_hp = [d for d in damage if d[5] is not None and d[6]]
    if not damage or len(with_hp) / len(damage) < MIN_HP_SHARE:
        reasons.append("no HP data on most hits")
    distinct: dict[int, set] = {}
    for d in with_hp:
        distinct.setdefault(d[1], set()).add(d[5])
    missing = [p for p in player_ids if len(distinct.get(p, ())) < MIN_DISTINCT_HP]
    if missing:
        reasons.append(f"{len(missing)} of {len(player_ids)} players without real HP data")
    if group_ehp and statistics.pstdev(group_ehp) < MIN_GROUP_STD:
        reasons.append("group HP never moves")
    if ref_duration_s and duration_s < ref_duration_s * MIN_DURATION_RATIO:
        reasons.append("run much shorter than yours (partial upload?)")
    return not reasons, reasons
