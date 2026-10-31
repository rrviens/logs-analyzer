"""Finding comparable top logs: same spec, same dungeon/boss, similar key level."""
from concurrent.futures import ThreadPoolExecutor

from app.reports import Fight, Player, get_fights, get_players
from app.wcl import WCLClient

RESOLVE_WORKERS = 6  # small fights/players queries - light on memory, so more at once is fine

RANKINGS_QUERY = """
query($encounterID: Int!, $className: String, $specName: String,
      $metric: CharacterRankingMetricType, $bracket: Int, $page: Int) {
  worldData { encounter(id: $encounterID) {
    characterRankings(className: $className, specName: $specName,
                      metric: $metric, bracket: $bracket, page: $page)
  } }
}
"""

RANKINGS_TTL_S = 6 * 3600


def top_rankings(
    client: WCLClient,
    encounter_id: int,
    class_name: str,
    spec_name: str,
    bracket: int | None = None,
    metric: str = "hps",
    page: int = 1,
) -> list[dict]:
    data = client.query(
        RANKINGS_QUERY,
        {
            "encounterID": encounter_id,
            "className": class_name,
            "specName": spec_name,
            "metric": metric,
            "bracket": bracket,
            "page": page,
        },
        ttl=RANKINGS_TTL_S,
    )
    return data["worldData"]["encounter"]["characterRankings"].get("rankings", [])


def comparable_fights(
    client: WCLClient,
    fight: Fight,
    class_name: str,
    spec_name: str,
    count: int = 10,
    key_range: int = 1,
) -> list[tuple[Fight, Player]]:
    """Top logs for the same encounter. For M+ it searches the run's key level first,
    then widens by +-key_range.

    WCL's M+ bracket argument is offset by one: bracket N returns +(N+1) keys, so the
    returned bracketData (the real key level) is checked too."""
    levels: list[int | None] = [None]
    if fight.keystone_level:
        lvl = fight.keystone_level
        levels = [lvl] + [l for d in range(1, key_range + 1) for l in (lvl + d, lvl - d)]

    def resolve(r: dict) -> tuple[Fight, Player] | None:
        code, fid = r["report"]["code"], r["report"]["fightID"]
        try:
            other = next(f for f in get_fights(client, code) if f.id == fid)
            return other, next(p for p in get_players(client, other) if p.name == r["name"])
        except StopIteration:
            return None

    results: list[tuple[Fight, Player]] = []
    seen: set[tuple[str, int]] = set()
    with ThreadPoolExecutor(RESOLVE_WORKERS) as pool:  # two small queries per log: in parallel, ranking order kept
        for level in levels:
            bracket = level - 1 if level else None
            rows = []
            for r in top_rankings(client, fight.encounter_id, class_name, spec_name, bracket):
                report = r.get("report") or {}
                key = (report.get("code"), report.get("fightID"))
                if level and r.get("bracketData") != level:
                    continue
                if None in key or key in seen or key == (fight.report_code, fight.id):
                    continue
                seen.add(key)
                rows.append(r)
                if len(rows) >= count - len(results) + 2:  # a couple spare for logs that don't resolve
                    break
            results += [fp for fp in pool.map(resolve, rows) if fp]
            if len(results) >= count:
                return results[:count]
    return results
