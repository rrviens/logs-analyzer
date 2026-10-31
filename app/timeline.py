"""Per-second group health timeline reconstructed from damage and healing rows.

Damage rows carry the target's HP after the hit (ground truth). Between them, HP is
moved forward by effective healing and by damage rows that lack resources.
"""
from dataclasses import dataclass, field

from app.runs import RunData

BIN_MS = 1000


@dataclass
class Timeline:
    start: int
    bin_ms: int
    group_ehp: list[float] = field(default_factory=list)  # mean (hp + shield) / max of living players
    min_ehp: list[float] = field(default_factory=list)  # lowest living player
    lowest_player: list[int] = field(default_factory=list)
    intake: list[float] = field(default_factory=list)  # damage incl. absorbed, per bin
    intake_frac: list[float] = field(default_factory=list)  # intake / group max HP
    healing: list[float] = field(default_factory=list)  # effective healing + absorbs, per bin
    healer_healing: list[float] = field(default_factory=list)
    dead: list[int] = field(default_factory=list)  # players dead at end of bin
    player_ehp: dict[int, list[float]] = field(default_factory=dict)
    unmitigated: list[float] = field(default_factory=list)  # raw damage before mitigation, per bin
    taken: list[float] = field(default_factory=list)  # damage + absorbed, only hits with a raw amount

    def bin_of(self, t: int) -> int:
        return max(0, min(len(self.group_ehp) - 1, (t - self.start) // self.bin_ms))

    def slice(self, start: int, end: int) -> range:
        return range(self.bin_of(start), self.bin_of(end) + 1)

    def at(self, t: int) -> float:
        return self.group_ehp[self.bin_of(t)]

    def mitigation(self, bins: range) -> float | None:
        """Share of raw incoming damage removed by armor, defensives etc. over the bins."""
        raw = sum(self.unmitigated[b] for b in bins)
        return 1 - sum(self.taken[b] for b in bins) / raw if raw else None


@dataclass
class _State:
    hp: float = 1.0
    max_hp: float = 1.0
    shield: float = 0.0
    alive: bool = True
    known: bool = False  # max HP seen yet

    @property
    def ehp(self) -> float:
        return min(1.0, (self.hp + self.shield) / self.max_hp) if self.known else 1.0


def build_timeline(run: RunData) -> Timeline:
    ids = run.player_ids
    state = {pid: _State() for pid in ids}
    n_bins = (run.end - run.start) // BIN_MS + 1
    tl = Timeline(start=run.start, bin_ms=BIN_MS)
    tl.intake = [0.0] * n_bins
    tl.healing = [0.0] * n_bins
    tl.healer_healing = [0.0] * n_bins
    tl.unmitigated = [0.0] * n_bins
    tl.taken = [0.0] * n_bins
    tl.player_ehp = {pid: [] for pid in ids}

    # (t, order, kind, row) - deaths sort after damage at the same timestamp
    stream = [(d[0], 0, "dmg", d) for d in run.damage]
    stream += [(h[0], 1, "heal", h) for h in run.heals]
    stream += [(d[0], 2, "death", d) for d in run.deaths]
    stream.sort(key=lambda x: (x[0], x[1]))

    i = 0
    for b in range(n_bins):
        bin_end = run.start + (b + 1) * BIN_MS
        while i < len(stream) and stream[i][0] < bin_end:
            _, _, kind, row = stream[i]
            i += 1
            if kind == "dmg":
                _, target, _, amount, absorbed, hp, max_hp, shield, unmitigated = row
                s = state[target]
                tl.intake[b] += amount + absorbed
                if unmitigated:
                    tl.unmitigated[b] += unmitigated
                    tl.taken[b] += amount + absorbed
                if hp is not None and max_hp:
                    s.hp, s.max_hp, s.shield, s.known = hp, max_hp, shield or 0, True
                    s.alive = hp > 0 or s.alive
                else:
                    s.hp = max(0.0, s.hp - amount)
            elif kind == "heal":
                _, source, target, amount, is_absorb = row
                tl.healing[b] += amount
                if source == run.healer_id:
                    tl.healer_healing[b] += amount
                s = state[target]
                if not is_absorb:
                    s.hp = min(s.max_hp, s.hp + amount)
                    s.alive = True
            else:
                s = state[row[1]]
                s.alive, s.hp, s.shield = False, 0.0, 0.0

        for pid, s in state.items():
            tl.player_ehp[pid].append(s.ehp if s.alive else 0.0)
        alive = [(pid, s) for pid, s in state.items() if s.alive]
        group_max = sum(s.max_hp for s in state.values() if s.known) or 1.0
        tl.intake_frac.append(tl.intake[b] / group_max)
        tl.dead.append(len(state) - len(alive))
        if alive:
            pid, low = min(alive, key=lambda x: x[1].ehp)
            tl.group_ehp.append(sum(s.ehp for _, s in alive) / len(alive))
            tl.min_ehp.append(low.ehp)
            tl.lowest_player.append(pid)
        else:
            tl.group_ehp.append(0.0)
            tl.min_ehp.append(0.0)
            tl.lowest_player.append(0)
    return tl
