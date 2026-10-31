"""Fetches everything needed to analyze one M+ run and trims it to compact rows.

Raw event pages are large, so they skip the query cache; the trimmed RunData is
cached instead.
"""
from dataclasses import asdict, dataclass

from app.reports import Fight, get_players
from app.specs import resto_shaman as rs
from app.wcl import WCLClient

CACHE_VERSION = 2

RUN_QUERY = """
query($code: String!) {
  reportData { report(code: $code) {
    fights { id dungeonPulls { id name startTime endTime encounterID enemyNPCs { gameID } } }
    masterData { abilities { gameID name } }
  } }
}
"""

EVENTS_QUERY = """
query($code: String!, $fightIDs: [Int], $start: Float, $end: Float, $dataType: EventDataType,
      $hostility: HostilityType, $sourceID: Int, $abilityID: Float, $resources: Boolean) {
  reportData { report(code: $code) {
    events(fightIDs: $fightIDs, startTime: $start, endTime: $end, dataType: $dataType,
           hostilityType: $hostility, sourceID: $sourceID, abilityID: $abilityID,
           includeResources: $resources, limit: 10000) {
      data
      nextPageTimestamp
    }
  } }
}
"""


@dataclass
class Pull:
    id: int
    name: str
    start: int
    end: int
    encounter_id: int  # 0 for trash
    npc_ids: list[int]

    @property
    def is_boss(self) -> bool:
        return self.encounter_id != 0


@dataclass
class RunData:
    code: str
    fight_id: int
    dungeon: str
    encounter_id: int
    keystone_level: int
    start: int
    end: int
    healer_id: int
    players: list[list]  # [id, name, role]
    pulls: list[Pull]
    damage: list[list]  # [t, target, ability, amount, absorbed, hp, max_hp, shield, unmitigated]
    heals: list[list]  # [t, source, target, amount, is_absorb]
    deaths: list[list]  # [t, target, killing_ability]
    casts: list[list]  # healer's casts [t, ability, target]
    riptide: list[list]  # healer's Riptide auras [t, type, target]
    abilities: list[list]  # [id, name]

    @property
    def player_ids(self) -> set[int]:
        return {p[0] for p in self.players}

    @property
    def tank_ids(self) -> set[int]:
        return {p[0] for p in self.players if p[2] == "tank"}

    def player_name(self, pid: int) -> str:
        return next((p[1] for p in self.players if p[0] == pid), f"#{pid}")

    def ability_name(self, aid: int) -> str:
        return next((a[1] for a in self.abilities if a[0] == aid), f"spell {aid}")


def fetch_run(client: WCLClient, fight: Fight, healer_id: int) -> RunData:
    key = f"run:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{healer_id}"
    cached = client.get_cached(key)
    if cached:
        cached["pulls"] = [Pull(**p) for p in cached["pulls"]]
        if cached["players"] and len(cached["players"][0]) < 3:  # cached before roles were stored
            cached["players"] = [[p.id, p.name, p.role] for p in get_players(client, fight)]
        return RunData(**cached)

    report = client.query(RUN_QUERY, {"code": fight.report_code})["reportData"]["report"]
    if not any(f["id"] == fight.id for f in report["fights"]):
        # Live-logged report: the cached copy predates this key. ttl=1 forces a refresh that is stored.
        report = client.query(RUN_QUERY, {"code": fight.report_code}, ttl=1)["reportData"]["report"]
    raw_fight = next(f for f in report["fights"] if f["id"] == fight.id)
    pulls = [
        Pull(
            id=p["id"],
            name=p["name"],
            start=p["startTime"],
            end=p["endTime"],
            encounter_id=p["encounterID"],
            npc_ids=sorted({n["gameID"] for n in p.get("enemyNPCs") or []}),
        )
        for p in raw_fight.get("dungeonPulls") or []
    ]
    players = get_players(client, fight)
    ids = {p.id for p in players}

    damage = [
        [e["timestamp"], e["targetID"], e.get("abilityGameID", 0), e.get("amount", 0), e.get("absorbed", 0),
         e.get("hitPoints"), e.get("maxHitPoints"), e.get("absorb", 0), e.get("unmitigatedAmount")]
        for e in _events(client, fight, "DamageTaken", hostility="Friendlies", resources=True)
        if e.get("type") == "damage" and e.get("targetID") in ids
    ]
    heals = [
        [e["timestamp"], e.get("sourceID"), e["targetID"], e.get("amount", 0), e["type"] == "absorbed"]
        for e in _events(client, fight, "Healing", hostility="Friendlies")
        if e.get("type") in ("heal", "absorbed") and e.get("targetID") in ids
    ]
    deaths = [
        [e["timestamp"], e["targetID"], e.get("killingAbilityGameID", 0)]
        for e in _events(client, fight, "Deaths", hostility="Friendlies")
        if e.get("targetID") in ids
    ]
    casts = [
        [e["timestamp"], e.get("abilityGameID", 0), e.get("targetID")]
        for e in _events(client, fight, "Casts", source_id=healer_id)
        if e.get("type") == "cast"
    ]
    riptide = [
        [e["timestamp"], e["type"], e.get("targetID")]
        for e in _events(client, fight, "Buffs", source_id=healer_id, ability_id=rs.RIPTIDE_ID)
    ]

    run = RunData(
        code=fight.report_code,
        fight_id=fight.id,
        dungeon=fight.name,
        encounter_id=fight.encounter_id,
        keystone_level=fight.keystone_level or 0,
        start=fight.start,
        end=fight.end,
        healer_id=healer_id,
        players=[[p.id, p.name, p.role] for p in players],
        pulls=pulls,
        damage=damage,
        heals=heals,
        deaths=deaths,
        casts=casts,
        riptide=riptide,
        abilities=[[a["gameID"], a["name"]] for a in report["masterData"]["abilities"]],
    )
    client.set_cached(key, asdict(run))
    return run


def _events(client: WCLClient, fight: Fight, data_type: str, hostility: str | None = None,
            source_id: int | None = None, ability_id: int | None = None, resources: bool = False) -> list[dict]:
    events: list[dict] = []
    start = fight.start
    while start is not None:
        data = client.query(
            EVENTS_QUERY,
            {
                "code": fight.report_code,
                "fightIDs": [fight.id],
                "start": start,
                "end": fight.end,
                "dataType": data_type,
                "hostility": hostility,
                "sourceID": source_id,
                "abilityID": ability_id,
                "resources": resources,
            },
            ttl=0,
        )
        page = data["reportData"]["report"]["events"]
        events.extend(page["data"])
        start = page["nextPageTimestamp"]
    return events
