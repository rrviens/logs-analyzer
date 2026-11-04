"""Fetches everything needed to analyze one M+ run and trims it to compact rows.

Raw event pages are large, so they skip the query cache; the trimmed RunData is
cached instead.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from app.reports import Fight, get_players
from app.specs.base import Spec
from app.wcl import WCLClient, WCLError

CACHE_VERSION = 2
UPLOAD_GAP_MS = 60_000  # events ending this long before the last pull does = the log is still uploading
UPLOAD_RETRY_S = 120
FETCH_STREAMS = int(os.environ.get("FETCH_STREAMS", "3"))  # event streams of one run fetched at once
# Events per request (WCL allows up to 10000). Smaller pages keep less in memory at once - a small host can then
# fetch more logs in parallel - at the cost of more round trips.
EVENT_PAGE = max(500, min(10000, int(os.environ.get("EVENT_PAGE", "10000"))))

RUN_QUERY = """
query($code: String!) {
  reportData { report(code: $code) {
    fights { id endTime dungeonPulls { id name startTime endTime encounterID enemyNPCs { gameID } } }
    masterData { abilities { gameID name } }
  } }
}
"""

EVENTS_QUERY = """
query($code: String!, $fightIDs: [Int], $start: Float, $end: Float, $dataType: EventDataType,
      $hostility: HostilityType, $sourceID: Int, $abilityID: Float, $resources: Boolean, $limit: Int) {
  reportData { report(code: $code) {
    events(fightIDs: $fightIDs, startTime: $start, endTime: $end, dataType: $dataType,
           hostilityType: $hostility, sourceID: $sourceID, abilityID: $abilityID,
           includeResources: $resources, limit: $limit) {
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
    hots: list[list]  # healer's maintenance HoT auras (Riptide for Resto Shaman) [t, type, target]
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


def _uploading(pulls: list[dict], *streams: list[list]) -> bool:
    """A live-logged key analyzed too soon: Warcraft Logs already lists every pull, but the events stop well before
    the last pull ends (there is always damage, healing and casting in a pull)."""
    last = max((rows[-1][0] for rows in streams if rows), default=0)
    return bool(pulls) and last < pulls[-1]["end"] - UPLOAD_GAP_MS


def fetch_run(client: WCLClient, fight: Fight, healer_id: int, spec: Spec) -> RunData:
    key = f"run:v{CACHE_VERSION}:{fight.report_code}:{fight.id}:{healer_id}"
    cached = client.get_cached(key)
    if cached and _uploading(cached["pulls"], cached["damage"], cached["heals"], cached["casts"]):
        client.forget_fight(fight.report_code, fight.id)  # saved mid-upload: fetch it all again
        cached = None
    if cached:
        cached["pulls"] = [Pull(**p) for p in cached["pulls"]]
        if "riptide" in cached:  # cached before specs: the field held Resto Shaman's Riptide
            cached["hots"] = cached.pop("riptide")
        if cached["players"] and len(cached["players"][0]) < 3:  # cached before roles were stored
            cached["players"] = [[p.id, p.name, p.role] for p in get_players(client, fight)]
        ids = {p.id for p in get_players(client, fight)}  # cached query; drops players who weren't really in the key
        cached["players"] = [p for p in cached["players"] if p[0] in ids]
        return RunData(**cached)

    report = client.query(RUN_QUERY, {"code": fight.report_code})["reportData"]["report"]
    if not any(f["id"] == fight.id and f.get("endTime") == fight.end for f in report["fights"]):
        # Live-logged report: the cached copy predates this key (or this fight grew since). ttl=1 forces a stored refresh.
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

    def damage():
        return [[e["timestamp"], e["targetID"], e.get("abilityGameID", 0), e.get("amount", 0), e.get("absorbed", 0),
                 e.get("hitPoints"), e.get("maxHitPoints"), e.get("absorb", 0), e.get("unmitigatedAmount")]
                for e in _events(client, fight, "DamageTaken", hostility="Friendlies", resources=True)
                if e.get("type") == "damage" and e.get("targetID") in ids]

    def heals():
        return [[e["timestamp"], e.get("sourceID"), e["targetID"], e.get("amount", 0), e["type"] == "absorbed"]
                for e in _events(client, fight, "Healing", hostility="Friendlies")
                if e.get("type") in ("heal", "absorbed") and e.get("targetID") in ids]

    def deaths():
        return [[e["timestamp"], e["targetID"], e.get("killingAbilityGameID", 0)]
                for e in _events(client, fight, "Deaths", hostility="Friendlies") if e.get("targetID") in ids]

    def casts():
        return [[e["timestamp"], e.get("abilityGameID", 0), e.get("targetID")]
                for e in _events(client, fight, "Casts", source_id=healer_id) if e.get("type") == "cast"]

    def hots():
        return [[e["timestamp"], e["type"], e.get("targetID")]
                for e in _events(client, fight, "Buffs", source_id=healer_id, ability_id=spec.maintenance.buff_id)
                ] if spec.maintenance else []

    # Independent event streams, FETCH_STREAMS at a time: the waits overlap, and each stream still trims page by
    # page, so memory grows by at most that many raw pages.
    with ThreadPoolExecutor(FETCH_STREAMS) as pool:
        damage, heals, deaths, casts, hots = [f.result() for f in [pool.submit(fn) for fn in
                                                                   (damage, heals, deaths, casts, hots)]]

    if _uploading([asdict(p) for p in pulls], damage, heals, casts):
        raise WCLError("Warcraft Logs is still processing the end of this key", rate_limited=True,
                       retry_after=UPLOAD_RETRY_S, uploading=True)
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
        hots=hots,
        abilities=[[a["gameID"], a["name"]] for a in report["masterData"]["abilities"]],
    )
    client.set_cached(key, asdict(run))
    return run


def _events(client: WCLClient, fight: Fight, data_type: str, hostility: str | None = None,
            source_id: int | None = None, ability_id: int | None = None, resources: bool = False):
    """Yields the events page by page: callers trim each event as it comes, so the raw pages (10,000 events
    each, with every field) never pile up in memory - a 512 MB host would otherwise run out on long keys."""
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
                "limit": EVENT_PAGE,
            },
            ttl=0,
        )
        page = data["reportData"]["report"]["events"]
        start = page["nextPageTimestamp"]
        del data
        yield from page["data"]
