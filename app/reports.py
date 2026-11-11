"""Fetching fights, players and events from a WCL report."""
import re
from dataclasses import dataclass

from app.wcl import WCLClient

REPORT_URL_RE = re.compile(r"reports/([A-Za-z0-9]+)(?:.*?[#?&]fight=(\d+|last))?")

FIGHTS_QUERY = """
query($code: String!) {
  reportData { report(code: $code) {
    title
    fights { id encounterID name startTime endTime kill keystoneLevel difficulty }
  } }
}
"""

PLAYERS_QUERY = """
query($code: String!, $fightIDs: [Int]) {
  reportData { report(code: $code) { playerDetails(fightIDs: $fightIDs) } }
}
"""

COMBATANT_QUERY = """
query($code: String!, $fightIDs: [Int]) {
  reportData { report(code: $code) { playerDetails(fightIDs: $fightIDs, includeCombatantInfo: true) } }
}
"""
TRINKET_SLOTS = (12, 13)

EVENTS_QUERY = """
query($code: String!, $fightIDs: [Int], $start: Float, $end: Float,
      $dataType: EventDataType, $sourceID: Int, $abilityID: Float) {
  reportData { report(code: $code) {
    events(fightIDs: $fightIDs, startTime: $start, endTime: $end, dataType: $dataType,
           sourceID: $sourceID, abilityID: $abilityID, limit: 10000) {
      data
      nextPageTimestamp
    }
  } }
}
"""


@dataclass
class Fight:
    report_code: str
    id: int
    encounter_id: int
    name: str
    start: int  # ms, relative to report start
    end: int
    kill: bool | None
    keystone_level: int | None
    difficulty: int | None

    @property
    def duration_s(self) -> float:
        return (self.end - self.start) / 1000


@dataclass
class Player:
    id: int
    name: str
    class_name: str
    spec: str | None
    role: str  # tank, healer, dps

    @property
    def label(self) -> str:
        """e.g. 'Augmentation Evoker'."""
        return f"{self.spec} {self.class_name}" if self.spec else self.class_name


def parse_report_url(url: str) -> tuple[str, str | None]:
    """Returns (report code, fight id or 'last' or None)."""
    m = REPORT_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a Warcraft Logs report URL: {url}")
    return m.group(1), m.group(2)


def get_fights(client: WCLClient, code: str, ttl: float | None = None) -> list[Fight]:
    """ttl: pass a short one for reports that may still be live-logging."""
    report = client.query(FIGHTS_QUERY, {"code": code}, ttl=ttl)["reportData"]["report"]
    return [
        Fight(
            report_code=code,
            id=f["id"],
            encounter_id=f["encounterID"],
            name=f["name"],
            start=f["startTime"],
            end=f["endTime"],
            kill=f.get("kill"),
            keystone_level=f.get("keystoneLevel"),
            difficulty=f.get("difficulty"),
        )
        for f in report["fights"]
    ]


def pick_fight(fights: list[Fight], fight_id: str | None) -> Fight:
    """Explicit fight id wins; otherwise the full M+ run (the fight with a keystone level)."""
    if fight_id == "last":
        return fights[-1]
    if fight_id:
        return next(f for f in fights if f.id == int(fight_id))
    keys = [f for f in fights if f.keystone_level]
    if not keys:
        raise ValueError("No M+ run found in report; pass a URL with #fight=<id>")
    return keys[-1]


def get_players(client: WCLClient, fight: Fight) -> list[Player]:
    data = client.query(PLAYERS_QUERY, {"code": fight.report_code, "fightIDs": [fight.id]})
    details = data["reportData"]["report"]["playerDetails"]
    details = details.get("data", details) if isinstance(details, dict) else {}
    details = details.get("playerDetails", details) if isinstance(details, dict) else {}
    if not isinstance(details, dict):  # WCL sends [] instead of {} when a fight has no player data
        details = {}
    players = []
    for group, role in (("tanks", "tank"), ("healers", "healer"), ("dps", "dps")):
        for p in details.get(group, []):
            if p.get("type") == "Unknown":
                continue  # in the fight's time window without playing it (joined as the key ended): no combat data
            specs = p.get("specs") or []
            players.append(
                Player(
                    id=p["id"],
                    name=p["name"],
                    # WCL writes "DeathKnight" / "DemonHunter"; data files use the in-game name
                    class_name=re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p["type"]),
                    spec=re.sub(r"(?<=[a-z])(?=[A-Z])", " ", specs[0]["spec"]) if specs else None,
                    role=role,
                )
            )
    return players


ICONS_QUERY = "query($code:String!){reportData{report(code:$code){masterData{abilities{gameID name icon}}}}}"
GENERIC_ICONS = ("trade_", "inv_misc_questionmark", "temp")


def ability_icons(client: WCLClient, code: str) -> tuple[dict[int, str], dict[str, str]]:
    """Spell icons from the report: by ability id, and by name (skipping generic icons - the same name can
    belong to a helper spell with a placeholder icon). Icon = file name without extension."""
    abilities = client.query(ICONS_QUERY, {"code": code})["reportData"]["report"]["masterData"]["abilities"]
    by_id, by_name = {}, {}
    for a in abilities:
        icon = (a.get("icon") or "").rsplit(".", 1)[0]
        if not icon:
            continue
        by_id[a["gameID"]] = icon
        if not icon.startswith(GENERIC_ICONS):
            by_name.setdefault(a["name"], icon)
    return by_id, by_name


def get_trinkets(client: WCLClient, fight: Fight) -> dict[int, list[str]]:
    """Equipped trinket names per player id."""
    data = client.query(COMBATANT_QUERY, {"code": fight.report_code, "fightIDs": [fight.id]})
    details = data["reportData"]["report"]["playerDetails"]
    details = details.get("data", details).get("playerDetails", details)
    out = {}
    for group in ("tanks", "healers", "dps"):
        for p in details.get(group, []):
            gear = (p.get("combatantInfo") or {}).get("gear") or []
            out[p["id"]] = [g["name"] for g in gear if g.get("slot") in TRINKET_SLOTS and g.get("name")]
    return out


def get_events(
    client: WCLClient,
    fight: Fight,
    data_type: str,
    source_id: int | None = None,
    ability_id: int | None = None,
) -> list[dict]:
    """All events of one type for a fight, following WCL pagination."""
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
                "sourceID": source_id,
                "abilityID": ability_id,
            },
        )
        page = data["reportData"]["report"]["events"]
        events.extend(page["data"])
        start = page["nextPageTimestamp"]
    return events

def player_realms(client: WCLClient, fight: Fight) -> list[dict]:
    """[{name, realm, region, class}] of the players in a fight (same cached query as get_players)."""
    data = client.query(PLAYERS_QUERY, {"code": fight.report_code, "fightIDs": [fight.id]})
    details = data["reportData"]["report"]["playerDetails"]
    details = details.get("data", details).get("playerDetails", details)
    raw = {p["id"]: p for group in ("tanks", "healers", "dps") for p in details.get(group, [])}
    return [{"name": p.name, "class": p.class_name, "realm": raw[p.id].get("server"), "region": raw[p.id].get("region")}
            for p in get_players(client, fight) if p.id in raw and raw[p.id].get("server") and raw[p.id].get("region")]
