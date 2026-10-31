"""Healing Wave vs Chain Heal: the situation at each cast and what each heal achieved.

Situation = how many players were injured just before the heal landed. Chain Heal pays
off when several are injured; Healing Wave when one player needs a big heal. The
pattern is learned from the comparable top logs rather than assumed.
"""
from dataclasses import asdict, dataclass

from app.healer_state import TRACKED_BUFFS, HealerState
from app.reports import Fight
from app.runs import RunData, _events
from app.timeline import Timeline
from app.wcl import WCLClient

SPELL_NAMES = ("Healing Wave", "Chain Heal")
CACHE_VERSION = 1
INJURED_EHP = 0.8
CH_BOUNCE_MS = 1500  # Chain Heal jumps land within this after the cast
HW_LAND_MS = 500
UNLEASH_WINDOW_MS = 10000

SITUATIONS = ("none", "one", "two", "group")  # injured players: 0, 1, 2, 3+


@dataclass
class HealCast:
    t: int
    spell: str  # "Healing Wave" / "Chain Heal"
    target: int | None
    situation: str
    injured: int
    lowest: float  # lowest living player's effective HP before the heal
    effective: float
    overheal: float
    targets_healed: int  # players that got effective healing
    after_unleash: bool
    after_swiftness: bool
    mana: float | None = None  # healer mana fraction at the cast
    buffs: tuple = ()  # tracked healing buffs active at the cast (healer_state.TRACKED_BUFFS)

    @property
    def overheal_share(self) -> float:
        total = self.effective + self.overheal
        return self.overheal / total if total else 0.0


def fetch_spell_heals(client: WCLClient, fight: Fight, run: RunData) -> list[list]:
    """The healer's Healing Wave / Chain Heal heal events: [t, target, amount, overheal, ability]."""
    key = f"spellheals:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{run.healer_id}"
    cached = client.get_cached(key)
    if cached is not None:
        return cached
    rows = []
    for aid, name in run.abilities:
        if name not in SPELL_NAMES:
            continue
        for e in _events(client, fight, "Healing", source_id=run.healer_id, ability_id=aid):
            if e.get("type") == "heal":
                rows.append([e["timestamp"], e.get("targetID"), e.get("amount", 0), e.get("overheal", 0), aid])
    rows.sort(key=lambda r: r[0])
    client.set_cached(key, rows)
    return rows


def heal_casts(run: RunData, tl: Timeline, heals: list[list], state: HealerState | None = None) -> list[HealCast]:
    names = {aid: name for aid, name in run.abilities if name in SPELL_NAMES}
    unleash = sorted(t for t, a, _ in run.casts if run.ability_name(a) == "Unleash Life")
    swiftness = sorted(t for t, a, _ in run.casts if run.ability_name(a) == "Nature's Swiftness")
    casts = [(t, aid, target) for t, aid, target in run.casts if aid in names]

    # Each heal event belongs to the latest cast of that spell before it (casts can overlap).
    landed_by: dict[int, list[list]] = {}
    for h in heals:
        owner = max((i for i, (t, aid, _) in enumerate(casts)
                     if aid == h[4] and t <= h[0] <= t + (CH_BOUNCE_MS if names[aid] == "Chain Heal" else HW_LAND_MS)),
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
        # Healing Wave hits a second target this patch, and Chain Heal lands a second set of
        # bounces ~0.7s later - count everything the cast did, on players only (not pets).
        landed = [h for h in landed_by.get(i, []) if h[1] in run.player_ids]
        out.append(HealCast(
            t=t, spell=spell, target=target,
            situation=SITUATIONS[min(injured, 3)], injured=injured,
            lowest=min(alive) if alive else 0.0,
            effective=sum(h[2] for h in landed), overheal=sum(h[3] for h in landed),
            targets_healed=sum(1 for h in landed if h[2] > 0),
            after_unleash=any(0 <= t - u <= UNLEASH_WINDOW_MS for u in unleash),
            after_swiftness=any(0 <= t - s <= 2000 for s in swiftness),
            # Buffs are read just before the cast: the cast itself often consumes them.
            mana=state.mana_at(t) if state else None,
            buffs=tuple(state.active(t - 100)) if state else (),
        ))
    return out


def buff_table(casts: list[HealCast]) -> dict[str, dict]:
    """Per tracked buff: heals cast while it was up, and the Chain Heal share of those."""
    out = {}
    for name in TRACKED_BUFFS:
        here = [c for c in casts if name in c.buffs]
        ch = [c.effective for c in here if c.spell == "Chain Heal"]
        hw = [c.effective for c in here if c.spell == "Healing Wave"]
        out[name] = {"casts": len(here),
                     "ch_share": len(ch) / len(here) if here else None,
                     "ch_effective": sum(ch) / len(ch) if ch else None,
                     "hw_effective": sum(hw) / len(hw) if hw else None}
    return out


def choice_table(casts: list[HealCast]) -> dict[str, dict]:
    """Per situation: how many heals, Chain Heal share, and each spell's average outcome."""
    out = {}
    for sit in SITUATIONS:
        here = [c for c in casts if c.situation == sit]
        row = {"casts": len(here), "ch_share": None}
        if here:
            row["ch_share"] = sum(1 for c in here if c.spell == "Chain Heal") / len(here)
        for spell, key in (("Chain Heal", "ch"), ("Healing Wave", "hw")):
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
