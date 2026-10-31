"""Web app: lists your recent keys, runs analyses in the background, shows findings.

Local (start.bat, AUTH_MODE=local): one user, the app's API client.
Hosted (AUTH_MODE=wcl): each visitor signs in with Warcraft Logs; their analyses run on their own token and
rate limit, their private logs stay in their own cache namespace, and settings and jobs are per visitor.

Run: python -m uvicorn app.web:app --port 8000
"""
import json
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app import auth
from app.config import AUTH_MODE, CACHE_PATH, DATA_DIR, PROJECT_ROOT, SECURE_COOKIES, wcl_credentials
from app.pipeline import AnalysisError, cache_key, run_analysis
from app.reports import get_fights, get_players
from app.specs import SPECS, spec_for
from app.study_runner import DEFAULT_RUNS, run_study, study_info
from app.wcl import WCLClient, WCLError

STATIC = PROJECT_ROOT / "app" / "static"
TOP_LOGS = 8
RECENT_REPORTS = 12
RECENT_TTL_S = 300  # recent reports may still be live-logging
RECENT_FIRST = 5  # reports checked on first load; "Load more" checks all RECENT_REPORTS
FINISHED_REPORT_MS = 24 * 3600 * 1000  # reports older than this are finished: fight lists cached for good
MIN_RUN_MIN = 5  # shorter "runs" are resets
MIN_UNTIMED_MIN = 10  # short untimed attempts are bricked keys, not worth reviewing
SAME_RUN_MS = 120_000
MAX_JOBS = 500  # finished jobs kept in memory

CHARACTER_QUERY = """
query($name: String!, $server: String!, $region: String!, $limit: Int) {
  characterData { character(name: $name, serverSlug: $server, serverRegion: $region) {
    id name server { name }
    recentReports(limit: $limit) { data { code startTime } }
  } }
}
"""

MY_CHARACTERS_QUERY = "{ userData { currentUser { characters { name level server { name region { slug } } } } } }"
MY_CHARACTERS_TTL_S = 3600

app = FastAPI()
app_client = WCLClient(*wcl_credentials(), CACHE_PATH)
jobs: dict[str, dict] = {}
study_job: dict = {}


@dataclass
class Who:
    """Who is asking: the API client to use (theirs when hosted) and where their settings live."""
    client: WCLClient
    user_id: int  # 0 = local mode
    user_name: str | None

    @property
    def settings_path(self):
        if not self.user_id:  # local mode keeps its old place
            return PROJECT_ROOT / "data" / "settings.json"
        return DATA_DIR / "users" / str(self.user_id) / "settings.json"


def who(request: Request) -> Who:
    if AUTH_MODE != "wcl":
        return Who(app_client, 0, None)
    s = auth.get_session(app_client, request.cookies.get(auth.SESSION_COOKIE))
    if not s:
        raise HTTPException(401, "Sign in with Warcraft Logs first.")
    return Who(app_client.for_user(s.token, s.user_id), s.user_id, s.user_name)


class Settings(BaseModel):
    name: str
    server: str
    region: str = "eu"


class AnalyzeRequest(BaseModel):
    url: str
    fresh: bool = False


def load_settings(w: Who) -> dict | None:
    return json.loads(w.settings_path.read_text()) if w.settings_path.exists() else None


def server_slug(server: str) -> str:
    """Blizzard realm slug: accents dropped ("Aggra (Português)" -> "aggra-portugues"), apostrophes and brackets gone."""
    plain = unicodedata.normalize("NFKD", server).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9-]", "", re.sub(r"\s+", "-", plain.strip().lower()))


def lookup_character(client: WCLClient, s: dict, ttl: float) -> dict | None:
    data = client.query(CHARACTER_QUERY, {"name": s["name"], "server": server_slug(s["server"]),
                                          "region": s["region"].lower(), "limit": RECENT_REPORTS}, ttl=ttl)
    return data["characterData"]["character"]


def new_job(w: Who, progress: list[str] | None = None) -> tuple[str, dict]:
    if len(jobs) > MAX_JOBS:  # drop the oldest finished jobs
        for k in [k for k, j in jobs.items() if j["status"] != "running"][:len(jobs) - MAX_JOBS]:
            jobs.pop(k, None)
    job_id = uuid.uuid4().hex
    job = {"status": "running", "progress": progress or [], "result": None, "error": None, "owner": w.user_id}
    jobs[job_id] = job
    return job_id, job


# ---------- pages, health, sign-in ----------

@app.get("/")
def index():
    # no-store: an open tab otherwise keeps serving an old page after an update
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/my-characters")
def my_characters(w: Who = Depends(who)):
    """The signed-in visitor's claimed Warcraft Logs characters, for the character picker. Local mode has no
    account (manual entry); sessions from before the view-user-profile scope need to sign in again."""
    if not w.user_id:
        return {"characters": [], "local": True}
    try:
        user = w.client.quick().query(MY_CHARACTERS_QUERY, ttl=MY_CHARACTERS_TTL_S)["userData"]["currentUser"]
    except (WCLError, httpx.HTTPError, KeyError, TypeError):  # e.g. a session from before the scope
        return {"characters": [], "relogin": True}
    chars = [{"name": c["name"], "server": c["server"]["name"], "region": c["server"]["region"]["slug"].lower(),
              "level": c.get("level") or 0} for c in (user or {}).get("characters") or [] if c.get("server")]
    return {"characters": sorted(chars, key=lambda c: (-c["level"], c["name"]))}


@app.get("/api/realms")
def realms():
    """Realm names for the Server autocomplete, from the hand-kept data/realms.json (no API calls)."""
    data = json.loads((PROJECT_ROOT / "data" / "realms.json").read_text(encoding="utf-8"))
    return {r: sorted(set(names)) for r, names in data.items() if not r.startswith("_")}


@app.exception_handler(WCLError)
def wcl_error(request: Request, e: WCLError):
    """A Warcraft Logs refusal is never a 500: the page shows the message with a "Try again"."""
    headers = {"Retry-After": str(round(e.retry_after))} if e.retry_after else {}
    return JSONResponse({"detail": str(e), "rate_limited": e.rate_limited}, status_code=429 if e.rate_limited else 503,
                        headers=headers)


@app.exception_handler(httpx.HTTPError)
def http_error(request: Request, e: httpx.HTTPError):
    """Network trouble or an error status from Warcraft Logs (token or API): a message, not a 500."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return JSONResponse({"detail": "Warcraft Logs didn't answer properly" + (f" ({status})" if status else "")
                         + " - it may be busy. Try again in a minute.", "rate_limited": status == 429},
                        status_code=429 if status == 429 else 503)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    if AUTH_MODE != "wcl":
        return {"mode": "local", "user": None}
    s = auth.get_session(app_client, request.cookies.get(auth.SESSION_COOKIE))
    return {"mode": "wcl", "user": s.user_name if s else None}


@app.get("/login")
def login():
    url, state = auth.start_login(app_client)
    r = RedirectResponse(url, status_code=302)
    r.set_cookie(auth.STATE_COOKIE, state, max_age=auth.STATE_TTL_S, httponly=True, secure=SECURE_COOKIES, samesite="lax")
    return r


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?signin=cancelled", status_code=302)
    try:
        s = auth.finish_login(app_client, code, state, request.cookies.get(auth.STATE_COOKIE))
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except (httpx.HTTPError, KeyError, TypeError, ValueError):  # WCL busy / rate limited during the token swap
        return RedirectResponse("/?signin=failed", status_code=302)
    r = RedirectResponse("/", status_code=302)
    r.delete_cookie(auth.STATE_COOKIE)
    r.set_cookie(auth.SESSION_COOKIE, s.id, max_age=int(s.expires - datetime.now().timestamp()), httponly=True,
                 secure=SECURE_COOKIES, samesite="lax")
    return r


@app.post("/logout")
def logout(request: Request):
    auth.end_session(app_client, request.cookies.get(auth.SESSION_COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.SESSION_COOKIE)
    return r


# ---------- your character and keys ----------

@app.get("/api/settings")
def get_settings(w: Who = Depends(who)):
    return load_settings(w) or {}


@app.post("/api/settings")
def save_settings(s: Settings, w: Who = Depends(who)):
    char = lookup_character(w.client.quick(), s.model_dump(), ttl=0)
    if not char:
        raise HTTPException(404, f"No character {s.name} on {s.server} ({s.region.upper()}) on Warcraft Logs.")
    saved = {"name": char["name"], "server": char["server"]["name"], "region": s.region.lower()}
    w.settings_path.parent.mkdir(parents=True, exist_ok=True)
    w.settings_path.write_text(json.dumps(saved))
    return saved


@app.get("/api/recent")
def recent(limit: int = RECENT_FIRST, w: Who = Depends(who)):
    """Your recent keys from the newest `limit` reports (the page asks for more with "Load more"). Reports older
    than a day can't change, so their fight lists come from the cache; on a WCL rate limit it returns what it has
    found so far with a note instead of failing."""
    s, client = load_settings(w), w.client.quick()
    if not s:
        return {"runs": []}
    char = lookup_character(client, s, ttl=RECENT_TTL_S)
    if not char:
        raise HTTPException(404, "Character not found on Warcraft Logs.")
    reports = sorted(char["recentReports"]["data"], key=lambda r: -r["startTime"])
    runs, seen, partial = [], [], None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    for rep in reports[:max(1, limit)]:
        try:
            runs += _report_runs(client, rep, s, seen, None if now_ms - rep["startTime"] > FINISHED_REPORT_MS else RECENT_TTL_S)
        except WCLError as e:
            partial = str(e)
            break
    runs.sort(key=lambda r: r["started"], reverse=True)
    return {"character": f"{char['name']} - {char['server']['name']}", "runs": runs, "partial": partial,
            "more": limit < len(reports)}


def _report_runs(client: WCLClient, rep: dict, s: dict, seen: list, ttl: float | None) -> list[dict]:
    """Your keys in one report (seen: keys already listed, as several group members upload the same run)."""
    runs = []
    for f in get_fights(client, rep["code"], ttl=ttl):
        if not f.keystone_level or f.duration_s < (MIN_RUN_MIN if f.kill else MIN_UNTIMED_MIN) * 60:
            continue
        start_ms = rep["startTime"] + f.start
        # The same key is often uploaded by several group members in separate reports.
        if any(d == (f.name, f.keystone_level) and abs(t - start_ms) < SAME_RUN_MS for d, t in seen):
            continue
        me_ = next((p for p in get_players(client, f) if p.name.lower() == s["name"].lower()), None)
        if not me_:
            continue  # someone else's key in a shared report
        seen.append(((f.name, f.keystone_level), start_ms))
        started = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        runs.append({
            "spec": me_.label,
            "analyzable": bool(spec_for(me_.class_name, me_.spec)),
            "url": f"https://www.warcraftlogs.com/reports/{rep['code']}#fight={f.id}",
            "dungeon": f.name,
            "level": f.keystone_level,
            "timed": bool(f.kill),
            "duration_min": round(f.duration_s / 60, 1),
            "started": started.isoformat(),
            "analyzed": client.get_cached(cache_key(rep["code"], f.id, s["name"], TOP_LOGS)) is not None,
        })
    return runs


@app.get("/api/budget")
def budget(w: Who = Depends(who)):
    r = w.client.quick().query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }", ttl=0)["rateLimitData"]
    return {"used": round(r["pointsSpentThisHour"]), "limit": r["limitPerHour"], "reset_in_s": r["pointsResetIn"]}


# ---------- analysis and comparison (background jobs, visible only to their owner) ----------

@app.post("/api/analyze")
def start_analysis(req: AnalyzeRequest, w: Who = Depends(who)):
    job_id, job = new_job(w)
    s = load_settings(w) or {}

    def work():
        try:
            job["result"] = run_analysis(w.client, req.url, s.get("name"), TOP_LOGS,
                                         progress=job["progress"].append, use_cache=not req.fresh)
            job["status"] = "done"
        except (AnalysisError, WCLError, ValueError, StopIteration) as e:
            job["status"], job["error"] = "error", str(e) or "Could not read that log."
        except Exception as e:  # surface API/network failures to the page instead of hanging
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, w: Who = Depends(who)):
    job = jobs.get(job_id)
    if not job or job["owner"] != w.user_id:
        raise HTTPException(404, "Unknown job")
    return {k: v for k, v in job.items() if k != "owner"}


# ---------- top-player study (shared data: run by the site owner when hosted) ----------

class StudyRequest(BaseModel):
    runs: int = DEFAULT_RUNS
    spec: str = SPECS[0].key


def _spec(key: str):
    s = next((s for s in SPECS if s.key == key), None)
    if not s:
        raise HTTPException(404, f"Unknown spec {key}.")
    return s


@app.get("/api/study")
def get_study(spec: str = SPECS[0].key, w: Who = Depends(who)):
    return {**study_info(_spec(spec)), "job": study_job, "can_run": AUTH_MODE != "wcl"}


@app.post("/api/study")
def start_study(req: StudyRequest, w: Who = Depends(who)):
    if AUTH_MODE == "wcl":  # one shared study per spec; the owner runs `python study.py` and deploys the result
        raise HTTPException(403, "The top-player study is maintained by the site owner.")
    spec = _spec(req.spec)
    if study_job.get("status") == "running":
        raise HTTPException(409, "A study is already running.")
    study_job.clear()
    study_job.update({"status": "running", "progress": [], "error": None})

    def work():
        try:
            run_study(app_client, spec, max(3, min(req.runs, 20)), progress=study_job["progress"].append)
            study_job["status"] = "done"
        except Exception as e:  # report to the page; cached runs make a retry cheap
            study_job["status"], study_job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return study_job
