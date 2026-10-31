"""Debuffs on players and dispels: who had what for how long, who dispelled whom, and
which of a player's own abilities clear a debuff (learned from the log - e.g. Spirit Walk
breaking roots, Rescue, Cloak of Shadows)."""
from dataclasses import dataclass

from app.reports import Fight
from app.runs import _events
from app.wcl import WCLClient

CACHE_VERSION = 1
SELF_CLEAR_BEFORE_MS, SELF_CLEAR_AFTER_MS = 1500, 500  # cast events can land just after the aura drops
DISPEL_MATCH_MS = 100


@dataclass
class Auras:
    debuffs: list[list]  # [target, ability, start, end]
    dispels: list[list]  # [t, source, target, removed ability, dispel ability]

    def spans(self, target: int, ability: int) -> list[list]:
        return [d for d in self.debuffs if d[0] == target and d[1] == ability]

    def on_at(self, target: int, t: int) -> list[list]:
        return [d for d in self.debuffs if d[0] == target and d[2] <= t <= d[3]]


def fetch_auras(client: WCLClient, fight: Fight, player_ids: set[int]) -> Auras:
    key = f"auras:v{CACHE_VERSION}:{fight.report_code}:{fight.id}"
    cached = client.get_cached(key)
    if cached is None:
        open_at: dict[tuple[int, int], int] = {}
        spans = []
        for e in _events(client, fight, "Debuffs", hostility="Friendlies"):
            tgt, aid = e.get("targetID"), e.get("abilityGameID", 0)
            if tgt not in player_ids:
                continue
            if e["type"] in ("applydebuff", "applydebuffstack", "refreshdebuff"):
                open_at.setdefault((tgt, aid), e["timestamp"])
            elif e["type"] == "removedebuff" and (tgt, aid) in open_at:
                spans.append([tgt, aid, open_at.pop((tgt, aid)), e["timestamp"]])
        spans += [[tgt, aid, start, fight.end] for (tgt, aid), start in open_at.items()]
        dispels = [[e["timestamp"], e.get("sourceID"), e.get("targetID"), e.get("extraAbilityGameID", 0), e.get("abilityGameID", 0)]
                   for e in _events(client, fight, "Dispels", hostility="Friendlies")
                   if e.get("type") == "dispel" and not e.get("isBuff")]
        cached = {"debuffs": sorted(spans, key=lambda s: s[2]), "dispels": dispels}
        client.set_cached(key, cached)
    return Auras(cached["debuffs"], cached["dispels"])


def top_debuff_handling(tops: list[tuple], names: set[str], tools: set[str], plural: str) -> dict[str, str]:
    """For each debuff name: how the top healers of the spec (plural: "Resto Shamans") got rid of it when it was on them,
    and whether they dispelled themselves before others. tops = [(RunData, Auras), ...]."""
    import statistics
    out = {}
    for name in names:
        methods: dict[str, int] = {}
        clear_s, self_first, waves, runs = [], 0, 0, 0
        for run, auras in tops:
            healer = run.healer_id
            casts = [[t, healer, aid] for t, aid, _ in run.casts]
            mine = [d for d in auras.debuffs if d[0] == healer and run.ability_name(d[1]) == name]
            if mine:
                runs += 1
            for span in mine:
                how = removed_by(auras, span, casts, run.ability_name, tools)
                label = "dispelled by another player" if how.startswith("dispel:") else how
                methods[label] = methods.get(label, 0) + 1
                clear_s.append((span[3] - span[2]) / 1000)
                wave = [d for d in auras.debuffs if d[1] == span[1] and abs(d[2] - span[2]) <= 1000 and d[0] != healer]
                firsts = sorted((x for x in auras.dispels if x[1] == healer and x[3] == span[1]
                                 and span[2] <= x[0] <= span[3] + 100), key=lambda x: x[0])
                if wave and firsts:
                    waves += 1
                    self_first += firsts[0][2] == healer
        n = sum(methods.values())
        if n < 3:
            continue
        mix = ", ".join(f"{k} {v}" for k, v in sorted(methods.items(), key=lambda x: -x[1]))
        line = (f"Top {plural} with {name} on themselves ({n} times in {runs} runs) got rid of it after "
                f"{statistics.median(clear_s):.1f}s median - {mix}.")
        if waves:
            line += f" When others had it too, they dispelled themselves first {self_first} of {waves} times."
        out[name] = line
    return out


def dispellable(*auras: Auras) -> set[int]:
    """Debuffs somebody dispelled at least once - so they can be dispelled."""
    return {d[3] for a in auras for d in a.dispels}


def removed_by(a: Auras, span: list, casts: list[list], name_of, tools: set[str] | None = None) -> str:
    """How one debuff span ended: 'self-dispel', 'dispel:<source id>', '<own tool>', or 'expired'."""
    target, ability, _, end = span
    for t, src, tgt, removed, _ in a.dispels:
        if tgt == target and removed == ability and abs(t - end) <= DISPEL_MATCH_MS:
            return "self-dispel" if src == target else f"dispel:{src}"
    own = [c for c in casts if c[1] == target and end - SELF_CLEAR_BEFORE_MS <= c[0] <= end + SELF_CLEAR_AFTER_MS
           and (tools is None or name_of(c[2]) in tools)]
    return name_of(own[-1][2]) if own else "expired"


def learn_self_clears(a: Auras, casts: list[list], name_of, tools: set[str]) -> dict[int, set[str]]:
    """Debuff -> own defensive/escape tools seen ending it (cast right as the debuff drops,
    with no dispel). Only names in `tools` count, so a rotation cast that happens to line
    up with a debuff expiring is never mistaken for a clear."""
    out: dict[int, set[str]] = {}
    for span in a.debuffs:
        target, _, _, end = span
        if removed_by(a, span, [], name_of) != "expired":
            continue  # dispelled
        near = [c for c in casts if c[1] == target and end - SELF_CLEAR_BEFORE_MS <= c[0] <= end + SELF_CLEAR_AFTER_MS
                and name_of(c[2]) in tools]
        if near:
            out.setdefault(span[1], set()).add(name_of(near[-1][2]))
    return out
