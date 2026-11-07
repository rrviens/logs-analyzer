"""The healer's mana and healing buffs over time (fetched separately, cached per run)."""
from bisect import bisect_right
from dataclasses import dataclass, field

from app.reports import Fight
from app.runs import RunData, _events
from app.specs.base import Spec
from app.wcl import WCLClient

CACHE_VERSION = 3  # the healer id fixes the spec, so the tracked buff list needs no key of its own
MANA_TYPE = 0


@dataclass
class HealerState:
    """Mana plus the spec's tracked buffs (spec.tracked_buffs) and passive cheat-death procs
    (spec.proc_buffs - some are logged as buffs, others only as a heal, so both are checked)."""
    mana: list[list] = field(default_factory=list)  # [t, fraction 0-1]
    buffs: dict[str, list[list]] = field(default_factory=dict)  # name -> [[start, end], ...]
    tracked: tuple = ()
    proc_names: tuple = ()
    stored: dict[str, list[list]] = field(default_factory=dict)  # spec.stored_cooldowns name -> [[stored, used/ended]]

    def __post_init__(self):
        self._times = [m[0] for m in self.mana]

    def mana_at(self, t: int) -> float | None:
        i = bisect_right(self._times, t) - 1
        return self.mana[i][1] if i >= 0 else (self.mana[0][1] if self.mana else None)

    def active(self, t: int) -> list[str]:
        return [n for n, spans in self.buffs.items() if n in self.tracked and any(a <= t <= b for a, b in spans)]

    def procs(self) -> dict[str, list[list]]:
        return {n: s for n, s in self.buffs.items() if n in self.proc_names}

    def stored_at(self, name: str, t: int) -> list | None:
        """The span during which `name` was stored at time t, if it was."""
        return next((s for s in self.stored.get(name, []) if s[0] <= t <= s[1]), None)


def fetch_healer_state(client: WCLClient, fight: Fight, run: RunData, spec: Spec) -> HealerState:
    tracked, procs = spec.tracked_buffs, spec.proc_buffs
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
            if name not in tracked + procs:
                continue
            if e["type"] in ("applybuff", "applybuffstack", "refreshbuff"):
                open_at.setdefault(name, e["timestamp"])
            elif e["type"] == "removebuff" and name in open_at:
                spans.setdefault(name, []).append([open_at.pop(name), e["timestamp"]])
        for name, start in open_at.items():
            spans.setdefault(name, []).append([start, fight.end])
        for aid, name in run.abilities:
            if name in procs and name not in spans:
                procs = [[e["timestamp"], e["timestamp"]] for e in _events(client, fight, "Healing", source_id=run.healer_id,
                                                                           ability_id=aid) if e.get("type") == "heal"]
                if procs:
                    spans[name] = procs
        cached = {"mana": mana, "buffs": spans}
        client.set_cached(key, cached)
    return HealerState(cached["mana"], cached["buffs"], tracked, procs)


def fetch_stored(client: WCLClient, fight: Fight, run: RunData, spec: Spec) -> dict[str, list[list]]:
    """When each of spec.stored_cooldowns was stored (applied) and used (removed), for your own run only - a small
    query per buff, cached apart from the healer state so peer and top logs aren't fetched again."""
    out = {}
    for aid, (name, _, _) in spec.stored_cooldowns.items():
        key = f"stored:v1:{fight.report_code}:{fight.id}:{run.healer_id}:{aid}"
        spans = client.get_cached(key)
        if spans is None:
            spans, start = [], None
            for e in _events(client, fight, "Buffs", source_id=run.healer_id, ability_id=aid):
                if e.get("targetID") != run.healer_id:
                    continue
                if e["type"] in ("applybuff", "applybuffstack", "refreshbuff") and start is None:
                    start = e["timestamp"]
                elif e["type"] == "removebuff" and start is not None:
                    spans.append([start, e["timestamp"]])
                    start = None
            if start is not None:
                spans.append([start, fight.end])
            client.set_cached(key, spans)
        out[name] = spans
    return out
