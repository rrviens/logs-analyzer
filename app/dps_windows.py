"""Missed DPS windows: stretches with no damage coming in where the group is healthy or already being healed
(totems, HoTs) and no big hit is coming - time a healer can spend on damage. Each window shows what you did with
it (damage casts, other casts, idle) and what you could have dealt there: your own Chain Lightning on 2+ enemies
or Lava Burst / Lightning Bolt on one (the spec's damage spells, average hits from this log) times the enemies
alive. Top healers' damage in their own calm moments is shown alongside (calm time to calm time, not pull to
pull, because groups pull differently)."""
import bisect
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
    mix: list | None = None  # [(spell, share)] peers' damage in calm moments with this many enemies

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
    aoe_ticks: int = 1  # hits on each enemy per AoE cast

    def dps(self, enemies: int) -> float:
        if enemies >= 2 and self.aoe_hit:
            return self.aoe_hit * self.aoe_ticks * min(enemies, self.aoe_targets) / self.aoe_cast_s
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
                    statistics.mean(st) if len(st) >= 5 else None, spec.dps_st_cast_s, spec.dps_aoe_ticks)
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
        if enemy_casts is not None:
            w.enemies = enemies_alive(enemy_casts, t0, t1)
            if model:
                w.potential = model.dps(w.enemies) * w.seconds
        out.append(w)
    return out


# ---------- what peers deal in calm moments, by enemies alive ----------
# Learned from top healers' own calm windows: their damage per second with 1, 2, 3-4 and 5+ enemies around, and
# which spells it came from. Being real casts, it already includes DoTs ticking, cooldowns, target caps and damage
# falloff on big packs - no per-spell rules needed.
MIN_BUCKET_S = 10  # a top run needs this much calm time in a bucket to count
MIX_MIN_SHARE = 0.08
GEAR_MIN_HITS = 5
GEAR_CLAMP = (0.5, 1.5)


def bucket(enemies: int | None) -> int:
    n = enemies or 1
    return 5 if n >= 5 else 3 if n >= 3 else n


BUCKET_LABEL = {1: "1 enemy", 2: "2 enemies", 3: "3-4 enemies", 5: "5+ enemies"}


@dataclass
class PeerRates:
    rate: dict[int, float]  # bucket -> median calm damage per second across top runs
    mix: dict[int, list]  # bucket -> [(spell, share of their damage)]
    hit: dict[str, float]  # spell -> top players' average hit, for gear scaling
    runs: dict[int, int]  # bucket -> top runs that had enough calm time in it


def learn_peer_rates(tops: list[tuple]) -> PeerRates:
    """tops: [(RunData, windows with enemies set, damage rows)] of top healers of the spec."""
    per_run: dict[int, list[float]] = {}
    by_spell: dict[int, dict[str, float]] = {}
    hits: dict[str, list[float]] = {}
    for run, windows, damage in tops:
        names = {a: n for a, n in run.abilities}
        for _, amount, aid in damage:
            hits.setdefault(names.get(aid, ""), []).append(amount)
        rows = sorted(damage)
        times = [r[0] for r in rows]
        acc: dict[int, list[float]] = {}
        for w in windows:
            k = bucket(w.enemies)
            a = acc.setdefault(k, [0.0, 0.0])
            a[0] += w.damage
            a[1] += w.seconds
            spells = by_spell.setdefault(k, {})
            for _, amount, aid in rows[bisect.bisect_left(times, w.start):bisect.bisect_left(times, w.end)]:
                n = names.get(aid, "")
                spells[n] = spells.get(n, 0) + amount
        for k, (dmg, secs) in acc.items():
            if secs >= MIN_BUCKET_S:
                per_run.setdefault(k, []).append(dmg / secs)
    mix = {}
    for k, spells in by_spell.items():
        total = sum(spells.values()) or 1
        mix[k] = [(n, round(v / total, 2)) for n, v in sorted(spells.items(), key=lambda x: -x[1]) if v / total >= MIX_MIN_SHARE][:3]
    return PeerRates({k: statistics.median(v) for k, v in per_run.items()}, mix,
                     {n: statistics.mean(v) for n, v in hits.items() if len(v) >= GEAR_MIN_HITS},
                     {k: len(v) for k, v in per_run.items()})


def gear_factor(run: RunData, damage: list[list], peers: PeerRates) -> float:
    """Your hit sizes vs the top players' on the same spells (weighted by how often you use each) - gear, key level
    and crit in one number, so their rates are scaled to what you can do."""
    names = {a: n for a, n in run.abilities}
    mine: dict[str, list[float]] = {}
    for _, amount, aid in damage:
        mine.setdefault(names.get(aid, ""), []).append(amount)
    num = den = 0.0
    for n, v in mine.items():
        if len(v) >= GEAR_MIN_HITS and n in peers.hit:
            num += sum(v)
            den += peers.hit[n] * len(v)
    return min(GEAR_CLAMP[1], max(GEAR_CLAMP[0], num / den)) if den else 1.0


def apply_peer_rates(windows: list[Window], peers: PeerRates, gear: float) -> None:
    """Your potential per window = what peers deal with that many enemies, at your gear. Falls back to the
    nearest smaller bucket peers have data for."""
    for w in windows:
        k = bucket(w.enemies)
        while k not in peers.rate and k > 1:
            k = {5: 3, 3: 2, 2: 1}[k]
        if k in peers.rate:
            w.potential = peers.rate[k] * gear * w.seconds
            w.mix = peers.mix.get(k)


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
