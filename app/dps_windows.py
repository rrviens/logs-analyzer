"""Missed DPS windows: stretches with no damage coming in where the group is healthy or already being healed
(totems, HoTs) and no big hit is coming - time a healer can spend on damage. Each window shows what you did with
it (damage casts, other casts, idle) and what you could have dealt there: your own Chain Lightning on 2+ enemies
or Lava Burst / Lightning Bolt on one (the spec's damage spells, average hits from this log) times the enemies
alive. Top healers' damage in their own calm moments is shown alongside (calm time to calm time, not pull to
pull, because groups pull differently)."""
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
HIT_LOOKAHEAD_MS = 3000  # a big group hit within this is not a safe moment to start a cast
ENEMY_PAD_MS = 3000  # enemies casting this close to a window count as alive in it
INTAKE_CALM = 0.01  # share of group max HP taken per second that still counts as no damage
GCD_S = 1.2  # rough GCD with haste, for "idle" time


@dataclass
class Window:
    start: int
    end: int
    damage: float
    dps_casts: int  # casts of spells that dealt damage
    other_casts: int  # everything else (heals into a healthy group, buffs, movement)
    enemies: int | None = None  # enemies alive around the window (a floor)
    potential: float | None = None  # damage you could have dealt here with your own hit sizes

    @property
    def missed(self) -> float | None:
        return max(0.0, self.potential - self.damage) if self.potential is not None else None

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
    """Per timeline bin: in a pull, little damage coming in, nobody low, no big group hit within HIT_LOOKAHEAD_MS,
    and either the group is healthy (CALM_EHP or the pull's top-group target, whichever is lower) or healing is
    already running (totems, HoTs ticking) - with nothing coming in, HP climbs on its own. targets: pull index ->
    the HP top groups hold on that pull; without it each pull uses its own 75th percentile."""
    n = len(tl.group_ehp)
    out = [False] * n
    for i, p in enumerate(run.pulls):
        bins = list(tl.slice(p.start, p.end))
        if not bins:
            continue
        own = sorted(tl.group_ehp[b] for b in bins)[int(len(bins) * 0.75)]
        need = min(CALM_EHP, (targets or {}).get(i, own))
        for b in bins:
            quiet = tl.intake_frac[b] <= INTAKE_CALM and tl.min_ehp[b] >= NOBODY_LOW
            out[b] = quiet and (tl.group_ehp[b] >= need or tl.healing[b] > tl.intake[b])
    hit_bins = sorted({tl.bin_of(t) for t in hits})
    look = HIT_LOOKAHEAD_MS // tl.bin_ms
    for hb in hit_bins:  # the seconds just before a big hit are for topping, not damage
        for b in range(max(0, hb - look), hb + 1):
            out[b] = False
    for b in range(1, n - 1):  # a single noisy second doesn't end a window
        if not out[b] and out[b - 1] and out[b + 1] and tl.min_ehp[b] >= NOBODY_LOW:
            out[b] = True
    return out


@dataclass
class DamageModel:
    """What the healer could have cast, from their own hits in this log (gear and crits included)."""
    aoe_hit: float | None  # average damage per target hit of the spec's AoE spell (Chain Lightning)
    aoe_targets: int
    aoe_cast_s: float
    st_cast: float | None  # average damage per cast of the single-target spells (Lava Burst, Lightning Bolt)
    st_cast_s: float

    def dps(self, enemies: int) -> float:
        if enemies >= 2 and self.aoe_hit:
            return self.aoe_hit * min(enemies, self.aoe_targets) / self.aoe_cast_s
        if enemies >= 1 and self.st_cast:
            return self.st_cast / self.st_cast_s
        return 0.0


def damage_model(run: RunData, damage: list[list], spec) -> DamageModel | None:
    if not spec.dps_aoe and not spec.dps_st:
        return None
    by_name: dict[str, list[float]] = {}
    for _, amount, aid in damage:
        by_name.setdefault(run.ability_name(aid), []).append(amount)
    aoe = by_name.get(spec.dps_aoe, [])
    st = [a for n in spec.dps_st for a in by_name.get(n, [])]
    m = DamageModel(statistics.mean(aoe) if len(aoe) >= 5 else None, spec.dps_aoe_targets, spec.dps_aoe_cast_s,
                    statistics.mean(st) if len(st) >= 5 else None, spec.dps_st_cast_s)
    return m if m.aoe_hit or m.st_cast else None


def enemies_alive(enemy_casts: list[list], t0: int, t1: int) -> int:
    """Distinct enemies casting around the window - a floor (pure melee mobs don't show)."""
    return len({src for t, _, src in enemy_casts if t0 - ENEMY_PAD_MS <= t <= t1 + ENEMY_PAD_MS and src})


def find_windows(run: RunData, tl: Timeline, calm: list[bool], damage: list[list],
                 model: DamageModel | None = None, enemy_casts: list[list] | None = None) -> list[Window]:
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
        w = Window(t0, t1, sum(d[1] for d in damage if t0 <= d[0] < t1),
                   sum(1 for a in casts if a in damage_ids), sum(1 for a in casts if a not in damage_ids))
        if model and enemy_casts is not None:
            w.enemies = enemies_alive(enemy_casts, t0, t1)
            w.potential = model.dps(w.enemies) * w.seconds
        out.append(w)
    return out


def calm_dps(windows: list[Window]) -> float | None:
    secs = sum(w.seconds for w in windows)
    return sum(w.damage for w in windows) / secs if secs >= 20 else None


def summarize(windows: list[Window], top_rate: float | None) -> dict:
    mine = calm_dps(windows)
    secs = sum(w.seconds for w in windows)
    if any(w.potential is not None for w in windows):  # what you could have cast
        missed = sum(w.missed or 0 for w in windows)
    else:  # no spell model: top healers' calm-time rate
        missed = sum(max(0.0, top_rate * w.seconds - w.damage) for w in windows) if top_rate else None
    return {"seconds": round(secs), "windows": len(windows), "your_dps": round(mine) if mine else None,
            "top_dps": round(top_rate) if top_rate else None, "missed": round(missed) if missed is not None else None,
            "idle_s": round(sum(w.idle_s for w in windows)), "other_casts": sum(w.other_casts for w in windows),
            "dps_casts": sum(w.dps_casts for w in windows)}


def median_rate(rates: list[float | None]) -> float | None:
    rates = [r for r in rates if r]
    return statistics.median(rates) if rates else None
