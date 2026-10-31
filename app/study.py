"""Studies top Resto Shaman runs per dungeon and builds an explicit context model:
which cooldowns they use on which pull and when, which group hits matter, how they
prepare for them, and what they answer them with.

Uses a light fetch (no healing events) so a whole season fits the API budget.
Pre-hit HP comes from damage snapshots: HP after the hit + the hit itself.
"""
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable

from app.baseline import top_rankings
from app.quality import check as quality_check
from app.reports import Fight, get_fights, get_players
from app.runs import RUN_QUERY, Pull, _events
from app.specs import resto_shaman as rs
from app.wcl import WCLClient

LIGHT_CACHE_VERSION = 1
MIN_CD_GAP_S = 45  # an ability whose casts are never closer than this is a cooldown
HIT_WINDOW_MS = 1500
HIT_MIN_TARGETS = 3
HIT_MIN_FRAC = 0.12
ANSWER_BEFORE_MS, ANSWER_AFTER_MS = 6000, 2000
PREP_WINDOW_MS = 4000
TRASH_MATCH_MIN = 0.5
MIN_SEGMENT_S = 10
PLAN_CLUSTER_S = 12  # casts this close across runs count as the same planned timing
PLAN_MIN_SHARE = 0.6


@dataclass
class LightRun:
    code: str
    fight_id: int
    player: str
    keystone_level: int
    start: int
    end: int
    pulls: list[Pull]
    tank_ids: list[int]
    damage: list[list]  # [t, target, ability, amount, absorbed, hp, max_hp]
    casts: list[list]  # [t, ability]
    riptide: list[list]  # [t, type, target]
    deaths: list[list]  # [t, target, ability]
    abilities: dict  # str(id) -> name


def fetch_light(client: WCLClient, fight: Fight, healer_id: int, healer_name: str) -> LightRun:
    key = f"light:v{LIGHT_CACHE_VERSION}:{fight.report_code}:{fight.id}:{healer_id}"
    cached = client.get_cached(key)
    if cached:
        cached["pulls"] = [Pull(**p) for p in cached["pulls"]]
        return LightRun(**cached)

    report = client.query(RUN_QUERY, {"code": fight.report_code})["reportData"]["report"]
    raw = next(f for f in report["fights"] if f["id"] == fight.id)
    pulls = [Pull(p["id"], p["name"], p["startTime"], p["endTime"], p["encounterID"],
                  sorted({n["gameID"] for n in p.get("enemyNPCs") or []}))
             for p in raw.get("dungeonPulls") or []]
    players = get_players(client, fight)
    ids = {p.id for p in players}
    run = LightRun(
        code=fight.report_code,
        fight_id=fight.id,
        player=healer_name,
        keystone_level=fight.keystone_level or 0,
        start=fight.start,
        end=fight.end,
        pulls=pulls,
        tank_ids=[p.id for p in players if p.role == "tank"],
        damage=[[e["timestamp"], e["targetID"], e.get("abilityGameID", 0), e.get("amount", 0),
                 e.get("absorbed", 0), e.get("hitPoints"), e.get("maxHitPoints")]
                for e in _events(client, fight, "DamageTaken", hostility="Friendlies", resources=True)
                if e.get("type") == "damage" and e.get("targetID") in ids],
        casts=[[e["timestamp"], e.get("abilityGameID", 0)]
               for e in _events(client, fight, "Casts", source_id=healer_id) if e.get("type") == "cast"],
        riptide=[[e["timestamp"], e["type"], e.get("targetID")]
                 for e in _events(client, fight, "Buffs", source_id=healer_id, ability_id=rs.RIPTIDE_ID)],
        deaths=[[e["timestamp"], e["targetID"], e.get("killingAbilityGameID", 0)]
                for e in _events(client, fight, "Deaths", hostility="Friendlies") if e.get("targetID") in ids],
        abilities={str(a["gameID"]): a["name"] for a in report["masterData"]["abilities"]},
    )
    client.set_cached(key, asdict(run))
    return run


def top_runs(client: WCLClient, encounter_id: int, count: int,
             before_fetch: Callable[[], None] = lambda: None, skipped: list | None = None) -> list[LightRun]:
    """Highest-ranked Resto Shaman runs for a dungeon, one per player, skipping logs that fail
    the data-quality gate (they're appended to `skipped`). before_fetch runs before each run is
    fetched (the study uses it to wait for API budget)."""
    runs, seen = [], set()
    for r in top_rankings(client, encounter_id, rs.CLASS_NAME, rs.SPEC_NAME):
        rep = r.get("report") or {}
        if r["name"] in seen or not rep.get("code"):
            continue
        before_fetch()
        try:
            fight = next(f for f in get_fights(client, rep["code"]) if f.id == rep["fightID"])
            me = next(p for p in get_players(client, fight) if p.name == r["name"])
        except StopIteration:
            continue
        seen.add(r["name"])
        light = fetch_light(client, fight, me.id, r["name"])
        ok, reasons = quality_check(light.damage, {p.id for p in get_players(client, fight)}, fight.duration_s)
        if not ok:
            if skipped is not None:
                skipped.append({"player": r["name"], "key": fight.keystone_level, "reasons": reasons})
            continue
        runs.append(light)
        if len(runs) >= count:
            break
    return runs


# ---------- analysis ----------

def discover_cooldowns(runs: list[LightRun]) -> dict[int, dict]:
    """Abilities every top player treats as a cooldown: cast in most runs, never back to back."""
    per_ability: dict[int, list[tuple[int, float]]] = {}
    for run in runs:
        by: dict[int, list[int]] = {}
        for t, a in run.casts:
            by.setdefault(a, []).append(t)
        for a, ts in by.items():
            ts.sort()
            gaps = [(b - x) / 1000 for x, b in zip(ts, ts[1:])]
            per_ability.setdefault(a, []).append((len(ts), min(gaps) if gaps else float("inf")))
    out = {}
    for a, rows in per_ability.items():
        if len(rows) < max(2, len(runs) // 2):
            continue
        min_gap = min(g for _, g in rows)
        if min_gap < MIN_CD_GAP_S:
            continue
        finite = [g for _, g in rows if g != float("inf")]
        out[a] = {
            "name": _name(runs, a),
            "runs_used": len(rows),
            "casts_per_run": statistics.median(n for n, _ in rows),
            "observed_cd_s": round(min(finite)) if finite else None,
        }
    return out


def cluster_segments(runs: list[LightRun]) -> list[dict]:
    """Groups the same pull across runs: bosses by encounter, trash by enemy set."""
    clusters: list[dict] = []
    for ri, run in enumerate(runs):
        for p in run.pulls:
            match = None
            for c in clusters:
                if p.is_boss and c["encounter_id"] == p.encounter_id:
                    match = c
                elif not p.is_boss and not c["encounter_id"]:
                    a, b = set(p.npc_ids), set(c["npc_ids"])
                    if a and b and len(a & b) / len(a | b) >= TRASH_MATCH_MIN:
                        match = c
                if match:
                    break
            if not match:
                match = {"name": p.name, "encounter_id": p.encounter_id, "npc_ids": p.npc_ids, "members": []}
                clusters.append(match)
            match["members"].append((ri, p))
    # order by typical position in the route
    for c in clusters:
        c["order"] = statistics.median(
            (p.start - runs[ri].start) / max(1, runs[ri].end - runs[ri].start) for ri, p in c["members"])
    clusters.sort(key=lambda c: c["order"])
    return clusters


def group_hits(run: LightRun, pull: Pull) -> list[dict]:
    max_hp: dict[int, float] = {}
    for d in run.damage:
        if d[6]:
            max_hp[d[1]] = max(max_hp.get(d[1], 0), d[6])
    by: dict[int, list[list]] = {}
    for d in run.damage:
        if pull.start <= d[0] < pull.end and d[2] != 1:
            by.setdefault(d[2], []).append(d)
    hits = []
    for a, rows in by.items():
        rows.sort(key=lambda r: r[0])
        i = 0
        while i < len(rows):
            t0, cluster = rows[i][0], []
            while i < len(rows) and rows[i][0] - t0 <= HIT_WINDOW_MS:
                cluster.append(rows[i])
                i += 1
            first: dict[int, list] = {}
            dmg: dict[int, float] = {}
            for r in cluster:
                first.setdefault(r[1], r)
                dmg[r[1]] = dmg.get(r[1], 0) + (r[3] + r[4]) / max_hp.get(r[1], 1)
            if len(dmg) < HIT_MIN_TARGETS or statistics.mean(dmg.values()) < HIT_MIN_FRAC:
                continue
            pre = [min(1.0, (r[5] + r[3]) / r[6]) for r in first.values() if r[5] is not None and r[6]]
            hits.append({"ability": a, "t": t0, "targets": len(dmg), "avg_frac": statistics.mean(dmg.values()),
                         "pre_hp": statistics.mean(pre) if pre else None})
    return sorted(hits, key=lambda h: h["t"])


def riptides_at(run: LightRun, t: int) -> int:
    active: set = set()
    for ts, kind, target in sorted(run.riptide, key=lambda r: r[0]):
        if ts > t:
            break
        if kind in ("applybuff", "refreshbuff"):
            active.add(target)
        elif kind == "removebuff":
            active.discard(target)
    return len(active)


def build_model(runs: list[LightRun], cooldowns: dict[int, dict]) -> dict:
    """Context model for one dungeon."""
    cd_ids = set(cooldowns)
    base_rate = Counter(a for run in runs for _, a in run.casts)
    total_casts = sum(base_rate.values()) or 1
    segments = []
    for c in cluster_segments(runs):
        members = c["members"]
        n = len({ri for ri, _ in members})
        if n < max(2, len(runs) // 2):
            continue
        if statistics.median((p.end - p.start) / 1000 for _, p in members) < MIN_SEGMENT_S:
            continue
        seg = {
            "name": c["name"],
            "encounter_id": c["encounter_id"],
            "npc_ids": c["npc_ids"],
            "runs": n,
            "median_duration_s": round(statistics.median((p.end - p.start) / 1000 for _, p in members)),
            "cooldowns": {},
            "group_hits": [],
            "deaths_per_run": round(sum(sum(1 for d in runs[ri].deaths if p.start <= d[0] < p.end)
                                        for ri, p in members) / n, 2),
        }
        # cooldown usage on this pull
        for a in cd_ids:
            times, run_times, proactive = [], [], 0
            for ri, p in members:
                run = runs[ri]
                for t, ab in run.casts:
                    if ab == a and p.start <= t < p.end:
                        times.append((t - p.start) / 1000)
                        run_times.append((ri, (t - p.start) / 1000))
                        before = _intake(run, t - 5000, t)
                        after = _intake(run, t, t + 6000)
                        proactive += after > 1.5 * before
            used_runs = len({ri for ri, p in members
                             if any(ab == a and p.start <= t < p.end for t, ab in runs[ri].casts)})
            if used_runs:
                seg["cooldowns"][cooldowns[a]["name"]] = {
                    "share_of_runs": round(used_runs / n, 2),
                    "median_time_s": round(statistics.median(times)),
                    "times_s": sorted(round(x) for x in times),
                    "proactive_share": round(proactive / len(times), 2),
                    "planned": _planned_timings(run_times, n) if c["encounter_id"] else [],
                }
        # recurring group hits
        hits_by: dict[int, list] = {}
        for ri, p in members:
            for h in group_hits(runs[ri], p):
                hits_by.setdefault(h["ability"], []).append((ri, p, h))
        for a, rows in hits_by.items():
            hit_runs = len({ri for ri, _, _ in rows})
            if hit_runs < max(2, n // 2):
                continue
            answered = Counter()
            prep = Counter()
            for ri, p, h in rows:
                run = runs[ri]
                for t, ab in run.casts:
                    if ab in cd_ids and h["t"] - ANSWER_BEFORE_MS <= t <= h["t"] + ANSWER_AFTER_MS:
                        answered[cooldowns[ab]["name"]] += 1
                    if h["t"] - PREP_WINDOW_MS <= t < h["t"]:
                        prep[ab] += 1
            lift = sorted(
                ((ab, cnt / len(rows), (cnt / len(rows)) / (base_rate[ab] / total_casts * 4)) for ab, cnt in prep.items()),
                key=lambda x: -x[2],
            )
            pre = [h["pre_hp"] for _, _, h in rows if h["pre_hp"] is not None]
            seg["group_hits"].append({
                "ability": a,
                "name": _name(runs, a),
                "runs": hit_runs,
                "per_run": round(len(rows) / hit_runs, 1),
                "avg_hit_pct": round(statistics.median(h["avg_frac"] for _, _, h in rows) * 100),
                "pre_hp_median": round(statistics.median(pre), 2) if pre else None,
                "pre_hp_p25": round(sorted(pre)[len(pre) // 4], 2) if pre else None,
                "riptides_median": statistics.median(riptides_at(runs[ri], h["t"]) for ri, _, h in rows),
                "answered_by": {k: round(v / len(rows), 2) for k, v in answered.most_common()},
                "prep_casts": [{"name": _name(runs, ab), "per_hit": round(per, 2), "lift": round(l, 1)}
                               for ab, per, l in lift[:4] if per >= 0.3 and l >= 1.5],
            })
        seg["group_hits"].sort(key=lambda h: -h["avg_hit_pct"])
        segments.append(seg)
    casts_per_min = {}
    for a in cd_ids:
        rates = [sum(1 for _, ab in r.casts if ab == a) / max(1.0, (r.end - r.start) / 60000) for r in runs]
        casts_per_min[cooldowns[a]["name"]] = round(statistics.median(rates), 3)
    return {"runs": [{"player": r.player, "key": r.keystone_level, "code": r.code, "fight": r.fight_id} for r in runs],
            "casts_per_min": casts_per_min,
            "segments": segments}


def _planned_timings(run_times: list[tuple[int, float]], n_runs: int) -> list[dict]:
    """Timings (seconds into a boss pull) that most top runs share for a cooldown."""
    out = []
    pending = sorted(run_times, key=lambda x: x[1])
    while pending:
        anchor = pending[0][1]
        group = [x for x in pending if x[1] - anchor <= PLAN_CLUSTER_S]
        runs_in = len({ri for ri, _ in group})
        if runs_in / n_runs >= PLAN_MIN_SHARE:
            out.append({"at_s": round(statistics.median(t for _, t in group)), "share": round(runs_in / n_runs, 2)})
        pending = [x for x in pending if x not in group]
    return out


def _intake(run: LightRun, a: int, b: int) -> float:
    return sum(d[3] + d[4] for d in run.damage if a <= d[0] < b)


def _name(runs: list[LightRun], a: int) -> str:
    for r in runs:
        if str(a) in r.abilities:
            return r.abilities[str(a)]
    return f"spell {a}"
