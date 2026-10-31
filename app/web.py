"""Local web app: lists your recent keys, runs analyses in the background, shows findings.

Start with start.bat (or: python -m uvicorn app.web:app --port 8000).
"""
import json
import re
import threading
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import CACHE_PATH, PROJECT_ROOT, wcl_credentials
from app.pipeline import AnalysisError, cache_key, run_analysis
from app.baseline import comparable_fights, top_rankings
from app.compare import compare_runs
from app.quality import check as quality_check
from app.reports import get_fights, get_players, parse_report_url, pick_fight
from app.runs import fetch_run
from app.specs import resto_shaman as rs
from app.study_runner import DEFAULT_RUNS, run_study, study_info
from app.wcl import WCLClient

SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"
STATIC = PROJECT_ROOT / "app" / "static"
TOP_LOGS = 8
RECENT_REPORTS = 12
RECENT_TTL_S = 300  # recent reports may still be live-logging
MIN_RUN_MIN = 5  # shorter "runs" are resets
MIN_UNTIMED_MIN = 10  # short untimed attempts are bricked keys, not worth reviewing
SAME_RUN_MS = 120_000

CHARACTER_QUERY = """
query($name: String!, $server: String!, $region: String!, $limit: Int) {
  characterData { character(name: $name, serverSlug: $server, serverRegion: $region) {
    id name server { name }
    recentReports(limit: $limit) { data { code startTime } }
  } }
}
"""

app = FastAPI()
client = WCLClient(*wcl_credentials(), CACHE_PATH)
jobs: dict[str, dict] = {}
study_job: dict = {}


class Settings(BaseModel):
    name: str
    server: str
    region: str = "eu"


class AnalyzeRequest(BaseModel):
    url: str
    fresh: bool = False


def load_settings() -> dict | None:
    return json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else None


def server_slug(server: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", server.strip().lower().replace(" ", "-"))


def lookup_character(s: dict, ttl: float) -> dict | None:
    data = client.query(CHARACTER_QUERY, {"name": s["name"], "server": server_slug(s["server"]),
                                          "region": s["region"].lower(), "limit": RECENT_REPORTS}, ttl=ttl)
    return data["characterData"]["character"]


@app.get("/")
def index():
    # no-store: an open tab otherwise keeps serving an old page after an update
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/settings")
def get_settings():
    return load_settings() or {}


@app.post("/api/settings")
def save_settings(s: Settings):
    char = lookup_character(s.model_dump(), ttl=0)
    if not char:
        raise HTTPException(404, f"No character {s.name} on {s.server} ({s.region.upper()}) on Warcraft Logs.")
    saved = {"name": char["name"], "server": char["server"]["name"], "region": s.region.lower()}
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(saved))
    return saved


@app.get("/api/recent")
def recent():
    s = load_settings()
    if not s:
        return {"runs": []}
    char = lookup_character(s, ttl=RECENT_TTL_S)
    if not char:
        raise HTTPException(404, "Character not found on Warcraft Logs.")
    runs, seen = [], []
    for rep in char["recentReports"]["data"]:
        for f in get_fights(client, rep["code"], ttl=RECENT_TTL_S):
            if not f.keystone_level or f.duration_s < (MIN_RUN_MIN if f.kill else MIN_UNTIMED_MIN) * 60:
                continue
            start_ms = rep["startTime"] + f.start
            # The same key is often uploaded by several group members in separate reports.
            if any(d == (f.name, f.keystone_level) and abs(t - start_ms) < SAME_RUN_MS for d, t in seen):
                continue
            me = next((p for p in get_players(client, f) if p.name.lower() == s["name"].lower()), None)
            if not me:
                continue  # someone else's key in a shared report
            seen.append(((f.name, f.keystone_level), start_ms))
            started = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            runs.append({
                "spec": me.label if me else None,
                "analyzable": bool(me and me.class_name == rs.CLASS_NAME and me.spec == rs.SPEC_NAME),
                "url": f"https://www.warcraftlogs.com/reports/{rep['code']}#fight={f.id}",
                "dungeon": f.name,
                "level": f.keystone_level,
                "timed": bool(f.kill),
                "duration_min": round(f.duration_s / 60, 1),
                "started": started.isoformat(),
                "analyzed": client.get_cached(cache_key(rep["code"], f.id, s["name"], TOP_LOGS)) is not None,
            })
    runs.sort(key=lambda r: r["started"], reverse=True)
    return {"character": f"{char['name']} - {char['server']['name']}", "runs": runs}


@app.get("/api/budget")
def budget():
    r = client.query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }", ttl=0)["rateLimitData"]
    return {"used": round(r["pointsSpentThisHour"]), "limit": r["limitPerHour"], "reset_in_s": r["pointsResetIn"]}


@app.post("/api/analyze")
def start_analysis(req: AnalyzeRequest):
    job_id = uuid.uuid4().hex[:8]
    job = {"status": "running", "progress": [], "result": None, "error": None}
    jobs[job_id] = job
    s = load_settings() or {}

    def work():
        try:
            job["result"] = run_analysis(client, req.url, s.get("name"), TOP_LOGS,
                                         progress=job["progress"].append, use_cache=not req.fresh)
            job["status"] = "done"
        except (AnalysisError, ValueError, StopIteration) as e:
            job["status"], job["error"] = "error", str(e) or "Could not read that log."
        except Exception as e:  # surface API/network failures to the page instead of hanging
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


class StudyRequest(BaseModel):
    runs: int = DEFAULT_RUNS


@app.get("/api/study")
def get_study():
    return {**study_info(), "job": study_job}


@app.post("/api/study")
def start_study(req: StudyRequest):
    if study_job.get("status") == "running":
        raise HTTPException(409, "A study is already running.")
    study_job.clear()
    study_job.update({"status": "running", "progress": [], "error": None})

    def work():
        try:
            run_study(client, max(3, min(req.runs, 20)), progress=study_job["progress"].append)
            study_job["status"] = "done"
        except Exception as e:  # report to the page; cached runs make a retry cheap
            study_job["status"], study_job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return study_job


class CompareRequest(BaseModel):
    url: str
    other_url: str


@app.get("/api/compare/candidates")
def compare_candidates(url: str):
    """Top logs for the same dungeon and key level that pass the data-quality gate."""
    code, fid = parse_report_url(url)
    fight = pick_fight(get_fights(client, code), fid)
    ranks = {r["name"]: r for lvl in {fight.keystone_level - 1, fight.keystone_level, fight.keystone_level + 1}
             for r in top_rankings(client, fight.encounter_id, rs.CLASS_NAME, rs.SPEC_NAME, lvl - 1)}
    out = []
    for f, p in comparable_fights(client, fight, rs.CLASS_NAME, rs.SPEC_NAME, 12):
        run = fetch_run(client, f, p.id) if client.get_cached(f"run:v2:{f.report_code}:{f.id}:{p.id}") else None
        if run:
            ok, _ = quality_check(run.damage, run.player_ids, f.duration_s, fight.duration_s)
            if not ok:
                continue
        server = (ranks.get(p.name) or {}).get("server") or {}
        out.append({"player": p.name, "key": f.keystone_level, "timed": bool(f.kill),
                    "region": (server.get("region") or "").upper(), "duration_min": round(f.duration_s / 60, 1),
                    "url": f"https://www.warcraftlogs.com/reports/{f.report_code}#fight={f.id}"})
    return {"candidates": out}


@app.post("/api/compare")
def start_compare(req: CompareRequest):
    job_id = uuid.uuid4().hex[:8]
    job = {"status": "running", "progress": ["Comparing..."], "result": None, "error": None}
    jobs[job_id] = job
    s = load_settings() or {}

    def work():
        try:
            job["result"] = compare_runs(client, req.url, req.other_url, s.get("name"))
            job["status"] = "done"
        except Exception as e:  # surface to the page
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Unknown job")
    return jobs[job_id]
