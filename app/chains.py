"""Chain pulls: the next pack engaged with no breather after the last one - and how risky that moment was
(damage-over-time debuffs from the old pack still ticking, group HP, your mana, cooldowns down) compared
with top groups at the same spot."""
import statistics
from dataclasses import dataclass, field

from app.auras import Auras
from app.context import CooldownTracker
from app.runs import RunData
from app.segments import MELEE_ID, match_pull

CHAIN_GAP_S = 5  # next pull starts this soon after the last one ended...
STILL_FIGHTING_S = 3  # ...and the group took damage this close to it, or the old pack kept hitting after it
SPILL_MIN_HITS = 2
OUTCOME_S = 20  # what happened right after the chain
RISK_HP_GAP = 0.10  # group HP this far under top groups at the same pull start
LOW_MANA = 0.35
BAD_LOWEST, BAD_GROUP = 0.25, 0.5  # a chain "hurt" when someone fell under 25% or the group under 50%


@dataclass
class Chain:
    t: int  # when the new pull started
    prev_idx: int
    idx: int
    spill: list[str]  # old pack's abilities still hitting after the new pull started
    dots: list[str]  # "Bleed on Name" - debuffs from the old pack still ticking
    group_hp: float
    top_hp: float | None
    mana: float | None
    cds_down: list[str]
    top_chained: tuple[int, int] | None  # (top runs that chained here, top runs with this pull)
    deaths: int
    min_lowest: float
    min_group: float
    risks: list[str] = field(default_factory=list)

    @property
    def risky(self) -> bool:
        """DoTs ticking, group low vs top groups, or low mana - a cooldown being down alone is just context."""
        return bool(self.dots) or (self.top_hp is not None and self.top_hp - self.group_hp >= RISK_HP_GAP) \
            or (self.mana is not None and self.mana < LOW_MANA)

    @property
    def hurt(self) -> bool:
        return bool(self.deaths) or self.min_lowest < BAD_LOWEST or self.min_group < BAD_GROUP


def _is_chain(run: RunData, a, b, player_cast_ids: set[int]) -> tuple[bool, list[str]]:
    if (b.start - a.end) / 1000 > CHAIN_GAP_S:
        return False, []
    prev_ab = {d[2] for d in run.damage if a.start <= d[0] < a.end - 5000 and d[2] != MELEE_ID} - player_cast_ids
    later_ab = {d[2] for d in run.damage if b.start + 20_000 <= d[0] <= b.end}
    spill = [d for d in run.damage if b.start <= d[0] <= b.start + 15_000 and d[2] in prev_ab - later_ab]
    last_dmg = max((d[0] for d in run.damage if a.start <= d[0] <= b.start), default=0)
    fighting = b.start - last_dmg <= STILL_FIGHTING_S * 1000
    return (fighting or len(spill) >= SPILL_MIN_HITS), sorted({run.ability_name(d[2]) for d in spill})


def find_chains(run: RunData, tl, auras: Auras, state, tops: list[tuple], group_casts: list[list],
                spec) -> list[Chain]:
    """tops = [(TopRun, Timeline), ...]."""
    cast_ids = {r[2] for r in group_casts}
    cds = CooldownTracker(run, spec.healing_cooldowns)
    out = []
    for i in range(1, len(run.pulls)):
        a, b = run.pulls[i - 1], run.pulls[i]
        chained, spill = _is_chain(run, a, b, cast_ids)
        if not chained:
            continue
        t = b.start
        dots = sorted({f"{run.ability_name(d[1])} on {run.player_name(d[0])}" for d in auras.debuffs
                       if a.start <= d[2] < t <= d[3]
                       and any(x[1] == d[0] and x[2] == d[1] and t <= x[0] <= d[3] for x in run.damage)})
        bin_t = tl.bin_of(t)
        group_hp = tl.group_ehp[max(0, bin_t - 1)]
        top_hp, chained_tops, seen = [], 0, 0
        for tr, otl in tops:
            p = match_pull(b, tr.run)
            if not p:
                continue
            seen += 1
            top_hp.append(otl.group_ehp[max(0, otl.bin_of(p.start) - 1)])
            k = tr.run.pulls.index(p)
            if k and _is_chain(tr.run, tr.run.pulls[k - 1], p, set())[0]:
                chained_tops += 1
        b_end = min(tl.bin_of(t + OUTCOME_S * 1000), len(tl.group_ehp) - 1)
        c = Chain(t, i - 1, i, spill, dots, group_hp, statistics.median(top_hp) if top_hp else None,
                  state.mana_at(t) if state and state.mana else None,
                  [cds.name(aid) for aid in cds.casts if not cds.ready(aid, t)],
                  (chained_tops, seen) if seen else None,
                  sum(1 for d in run.deaths if t <= d[0] <= t + OUTCOME_S * 1000),
                  min(tl.min_ehp[bin_t:b_end + 1] or [1.0]), min(tl.group_ehp[bin_t:b_end + 1] or [1.0]))
        if dots:
            c.risks.append(f"{len(dots)} damage-over-time debuff{'s' if len(dots) > 1 else ''} still ticking: "
                           + ", ".join(dots[:3]) + ("..." if len(dots) > 3 else ""))
        if c.top_hp is not None and c.top_hp - group_hp >= RISK_HP_GAP:
            c.risks.append(f"group at {group_hp:.0%} vs {c.top_hp:.0%} for peer groups starting this pull")
        if c.mana is not None and c.mana < LOW_MANA:
            c.risks.append(f"your mana {c.mana:.0%}")
        if c.cds_down:
            c.risks.append(" and ".join(c.cds_down) + " on cooldown")
        out.append(c)
    return out
