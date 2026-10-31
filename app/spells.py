"""Single-target vs group heal (Healing Wave vs Chain Heal for Resto Shaman): the situation at each
cast and what each heal achieved.

Situation = how many players were injured just before the heal landed. The group heal pays off
when several are injured; the single-target heal when one player needs a big heal. The pattern
is learned from the comparable top logs rather than assumed.

Table keys: "ch_*" = the spec's group heal, "hw_*" = its single-target heal.
"""
from dataclasses import asdict, dataclass

from app.healer_state import HealerState
from app.reports import Fight
from app.runs import RunData, _events
from app.specs.base import Spec, SpellPair
from app.timeline import Timeline
from app.wcl import WCLClient

CACHE_VERSION = 1
INJURED_EHP = 0.8
SWIFTNESS_WINDOW_MS = 2000

SITUATIONS = ("none", "one", "two", "group")  # injured players: 0, 1, 2, 3+


@dataclass
class HealCast:
    t: int
    spell: str  # the pair's single or group heal
    target: int | None
    situation: str
    injured: int
    lowest: float  # lowest living player's effective HP before the heal
    effective: float
    overheal: float
    targets_healed: int  # players that got effective healing
    after_unleash: bool  # within the empower window of the pair's empower cast
    after_swiftness: bool  # right after the pair's instant-cast enabler
    mana: float | None = None  # healer mana fraction at the cast
    buffs: tuple = ()  # spec.tracked_buffs active at the cast

    @property
    def overheal_share(self) -> float:
        total = self.effective + self.overheal
        return self.overheal / total if total else 0.0


def fetch_spell_heals(client: WCLClient, fight: Fight, run: RunData, spec: Spec) -> list[list]:
    """The healer's heal events from the spell pair: [t, target, amount, overheal, ability]."""
    pair = spec.spell_pair
    if not pair:
        return []
    key = f"spellheals:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{run.healer_id}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    rows = []
    for aid, name in run.abilities:
        if name not in (pair.single, pair.group):
            continue
        for e in _events(client, fight, "Healing", source_id=run.healer_id, ability_id=aid):
            if e.get("type") == "heal":
                rows.append([e["timestamp"], e.get("targetID"), e.get("amount", 0), e.get("overheal", 0), aid])
    rows.sort(key=lambda r: r[0])
    client.set_cached(key, rows)
    return rows


def heal_casts(run: RunData, tl: Timeline, heals: list[list], state: HealerState | None, spec: Spec) -> list[HealCast]:
    pair = spec.spell_pair
    if not pair:
        return []
    names = {aid: name for aid, name in run.abilities if name in (pair.single, pair.group)}
    empower = sorted(t for t, a, _ in run.casts if pair.empower_cast and run.ability_name(a) == pair.empower_cast)
    instant = sorted(t for t, a, _ in run.casts if pair.instant_cast and run.ability_name(a) == pair.instant_cast)
    casts = [(t, aid, target) for t, aid, target in run.casts if aid in names]

    # Each heal event belongs to the latest cast of that spell before it (casts can overlap).
    landed_by: dict[int, list[list]] = {}
    for h in heals:
        owner = max((i for i, (t, aid, _) in enumerate(casts)
                     if aid == h[4] and t <= h[0] <= t + pair.land_ms.get(names[aid], 500)),
                    default=None)
        if owner is not None:
            landed_by.setdefault(owner, []).append(h)

    out = []
    for i, (t, aid, target) in enumerate(casts):
        spell = names[aid]
        # Lowest HP per player across the cast's second and the one before: 1s bins can
        # otherwise miss damage that landed during the cast itself.
        b = tl.bin_of(t)
        alive = [min(s[max(0, b - 1)], s[b]) for s in tl.player_ehp.values() if s[b] > 0]
        injured = sum(1 for v in alive if v < INJURED_EHP)
        # Count everything the cast did (cleave, bounces), on players only (not pets).
        landed = [h for h in landed_by.get(i, []) if h[1] in run.player_ids]
        out.append(HealCast(
            t=t, spell=spell, target=target,
            situation=SITUATIONS[min(injured, 3)], injured=injured,
            lowest=min(alive) if alive else 0.0,
            effective=sum(h[2] for h in landed), overheal=sum(h[3] for h in landed),
            targets_healed=sum(1 for h in landed if h[2] > 0),
            after_unleash=any(0 <= t - u <= pair.empower_window_ms for u in empower),
            after_swiftness=any(0 <= t - s <= SWIFTNESS_WINDOW_MS for s in instant),
            # Buffs are read just before the cast: the cast itself often consumes them.
            mana=state.mana_at(t) if state else None,
            buffs=tuple(state.active(t - 100)) if state else (),
        ))
    return out


def buff_table(casts: list[HealCast], spec: Spec) -> dict[str, dict]:
    """Per tracked buff: heals cast while it was up, and the group-heal share of those."""
    pair, out = spec.spell_pair, {}
    for name in spec.tracked_buffs:
        here = [c for c in casts if name in c.buffs]
        ch = [c.effective for c in here if c.spell == pair.group]
        hw = [c.effective for c in here if c.spell == pair.single]
        out[name] = {"casts": len(here),
                     "ch_share": len(ch) / len(here) if here else None,
                     "ch_effective": sum(ch) / len(ch) if ch else None,
                     "hw_effective": sum(hw) / len(hw) if hw else None}
    return out


def choice_table(casts: list[HealCast], pair: SpellPair) -> dict[str, dict]:
    """Per situation: how many heals, group-heal share, and each spell's average outcome."""
    out = {}
    for sit in SITUATIONS:
        here = [c for c in casts if c.situation == sit]
        row = {"casts": len(here), "ch_share": None}
        if here:
            row["ch_share"] = sum(1 for c in here if c.spell == pair.group) / len(here)
        for spell, key in ((pair.group, "ch"), (pair.single, "hw")):
            mine = [c for c in here if c.spell == spell]
            row[f"{key}_overheal"] = (sum(c.overheal for c in mine) / max(1, sum(c.effective + c.overheal for c in mine))
                                      if mine else None)
            row[f"{key}_effective"] = sum(c.effective for c in mine) / len(mine) if mine else None
        manas = [c.mana for c in here if c.mana is not None]
        row["mana"] = sum(manas) / len(manas) if manas else None
        out[sit] = row
    return out


def as_rows(casts: list[HealCast]) -> list[dict]:
    return [asdict(c) for c in casts]
