"""The healer's mana and healing buffs over time (fetched separately, cached per run)."""
from bisect import bisect_right
from dataclasses import dataclass, field

from app.reports import Fight
from app.runs import RunData, _events
from app.wcl import WCLClient

CACHE_VERSION = 3
MANA_TYPE = 0
# Buffs on the healer that change how heals behave. Found in Season 2 Midnight logs;
# High Tide no longer exists. Edit if a patch changes them.
TRACKED_BUFFS = ("Tidal Waves", "Unleash Life", "Ascendance", "Coalescing Water", "Nature's Swiftness")
# Passive cheat-death / emergency procs - shown in death recaps as already spent. Some are
# logged as buffs, others only as a heal (Nature's Guardian), so both are checked.
PROC_BUFFS = ("Nature's Guardian",)


@dataclass
class HealerState:
    mana: list[list] = field(default_factory=list)  # [t, fraction 0-1]
    buffs: dict[str, list[list]] = field(default_factory=dict)  # name -> [[start, end], ...]

    def __post_init__(self):
        self._times = [m[0] for m in self.mana]

    def mana_at(self, t: int) -> float | None:
        i = bisect_right(self._times, t) - 1
        return self.mana[i][1] if i >= 0 else (self.mana[0][1] if self.mana else None)

    def active(self, t: int) -> list[str]:
        return [n for n, spans in self.buffs.items() if n in TRACKED_BUFFS and any(a <= t <= b for a, b in spans)]

    def procs(self) -> dict[str, list[list]]:
        return {n: s for n, s in self.buffs.items() if n in PROC_BUFFS}


def fetch_healer_state(client: WCLClient, fight: Fight, run: RunData) -> HealerState:
    key = f"healerstate:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{run.healer_id}"
    cached = client.get_cached(key)
    if cached is None:
        mana = []
        for e in _events(client, fight, "Casts", source_id=run.healer_id, resources=True):
            if e.get("resourceActor") != 1:
                continue
            for r in e.get("classResources") or []:
                if r.get("type") == MANA_TYPE and r.get("max"):
                    mana.append([e["timestamp"], r["amount"] / r["max"]])
        spans: dict[str, list[list]] = {}
        open_at: dict[str, int] = {}
        for e in _events(client, fight, "Buffs", source_id=run.healer_id):
            if e.get("targetID") != run.healer_id:
                continue
            name = run.ability_name(e.get("abilityGameID", 0))
            if name not in TRACKED_BUFFS + PROC_BUFFS:
                continue
            if e["type"] in ("applybuff", "applybuffstack", "refreshbuff"):
                open_at.setdefault(name, e["timestamp"])
            elif e["type"] == "removebuff" and name in open_at:
                spans.setdefault(name, []).append([open_at.pop(name), e["timestamp"]])
        for name, start in open_at.items():
            spans.setdefault(name, []).append([start, fight.end])
        for aid, name in run.abilities:
            if name in PROC_BUFFS and name not in spans:
                procs = [[e["timestamp"], e["timestamp"]] for e in _events(client, fight, "Healing", source_id=run.healer_id,
                                                                           ability_id=aid) if e.get("type") == "heal"]
                if procs:
                    spans[name] = procs
        cached = {"mana": mana, "buffs": spans}
        client.set_cached(key, cached)
    return HealerState(cached["mana"], cached["buffs"])
