"""Web app: lists your recent keys, runs analyses in the background, shows findings.

Local (start.bat, AUTH_MODE=local): one user, the app's API client.
Hosted (AUTH_MODE=wcl): each visitor signs in with Warcraft Logs; their analyses run on their own token and
rate limit, their private logs stay in their own cache namespace, and settings and jobs are per visitor.

Run: python -m uvicorn app.web:app --port 8000
"""
import json
import logging
import os
import re
import queue as queue_mod
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app import auth
from app.config import ADMIN_USER_IDS, AUTH_MODE, CACHE_PATH, DATA_DIR, PROJECT_ROOT, SECURE_COOKIES, wcl_credentials
from app.context_model import load_dungeon
from app.jobs import Jobs
from app.pipeline import RULES_VERSION, AnalysisError, cache_key, latest_key, run_analysis, stored_analysis
from app import raiderio
from app.reports import get_fights, get_players, parse_report_url, pick_fight, player_realms
from app.specs import SPECS, spec_for
from app.spellfacts import FACTS
from app.context_model import model_path
from app.study_runner import DEFAULT_RUNS, add_curves, covers, run_study, study_info
from app.wcl import WCLClient, WCLError

STATIC = PROJECT_ROOT / "app" / "static"
TOP_LOGS = 8
RECENT_REPORTS = 12
RECENT_TTL_S = 300  # recent reports may still be live-logging
RECENT_FIRST = 5  # reports checked on first load; "Load more" checks all RECENT_REPORTS
AUTO_ANALYZE = int(os.environ.get("AUTO_ANALYZE", "2"))  # newest unanalyzed keys queued in the background
FINISHED_REPORT_MS = 24 * 3600 * 1000  # reports older than this are finished: fight lists cached for good
MIN_RUN_MIN = 5  # shorter "runs" are resets
UPLOADS_QUERY = "query($uid: Int!, $limit: Int) { reportData { reports(userID: $uid, limit: $limit) { data { code startTime } } } }"
LOCAL_WCL_USER_ID = int(os.environ.get("WCL_USER_ID", "0") or 0)  # local mode: your WCL user id, to list your own uploads
MIN_UNTIMED_MIN = 2  # untimed attempts this short are resets; longer ones (even abandoned keys) are listed
ABANDONED_MIN = 20  # untimed and shorter than this = left before the end (a completed key runs longer)
SAME_RUN_MS = 120_000

CHARACTER_QUERY = """
query($name: String!, $server: String!, $region: String!, $limit: Int) {
  characterData { character(name: $name, serverSlug: $server, serverRegion: $region) {
    id name classID server { name }
    recentReports(limit: $limit) { data { code startTime } }
  } }
}
"""

MY_CHARACTERS_QUERY = "{ userData { currentUser { characters { name level classID server { name region { slug } } } } } }"
# Warcraft Logs' own class ids (gameData.classes) - alphabetical, not Blizzard's numbering.
CLASS_NAMES = {1: "Death Knight", 2: "Druid", 3: "Hunter", 4: "Mage", 5: "Monk", 6: "Paladin", 7: "Priest",
               8: "Rogue", 9: "Shaman", 10: "Warlock", 11: "Warrior", 12: "Demon Hunter", 13: "Evoker"}
MY_CHARACTERS_TTL_S = 3600
MY_CHARACTERS_SAVED = "my_characters_saved"  # per user (the cache namespace is the user's)

app = FastAPI()
log = logging.getLogger("uvicorn.error")  # shows in the Render logs
app_client = WCLClient(*wcl_credentials(), CACHE_PATH)
study_job: dict = {}


@dataclass
class Who:
    """Who is asking: the API client to use (theirs when hosted) and where their settings live."""
    client: WCLClient
    user_id: int  # 0 = local mode
    user_name: str | None

    @property
    def is_admin(self) -> bool:
        """Local mode: you run the app, so you're the admin. Hosted: your WCL user id is in ADMIN_USER_IDS."""
        return not self.user_id or self.user_id in ADMIN_USER_IDS

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


# ---------- pages, health, sign-in ----------

# The React front end (web/, built into app/static/next) is the site at /. /next/ and /classic (the retired page) keep
# working for old links. no-store on pages: an open tab otherwise keeps an old page after an update.
NEXT = STATIC / "next"


@app.get("/")
def index():
    if not (NEXT / "index.html").exists():
        raise HTTPException(503, "The front end isn't built yet (cd web && npm run build).")
    return FileResponse(NEXT / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/classic")
def classic():
    return RedirectResponse("/", status_code=301)


@app.get("/next/{path:path}")
def next_app(path: str = ""):
    asset = (NEXT / path).resolve()
    if path.startswith("assets/") and asset.is_file() and NEXT.resolve() in asset.parents:
        return FileResponse(asset, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    if not (NEXT / "index.html").exists():
        raise HTTPException(404, "The new front end isn't built yet (cd web && npm run build).")
    return FileResponse(NEXT / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/my-characters")
def my_characters(w: Who = Depends(who)):
    """The signed-in visitor's claimed Warcraft Logs characters, for the character picker. Local mode has no
    account (manual entry); sessions from before the view-user-profile scope need to sign in again."""
    if not w.user_id:
        return {"characters": [], "local": True}
    try:
        user = w.client.quick().query(MY_CHARACTERS_QUERY, ttl=MY_CHARACTERS_TTL_S)["userData"]["currentUser"]
    except (WCLError, httpx.HTTPError) as e:  # out of API points / busy: the last list we got, else "try later"
        log.warning("my characters unavailable (user %s): %s", w.user_id, e)
        return {**(w.client.get_cached(MY_CHARACTERS_SAVED) or {"characters": []}), "busy": True}
    except (KeyError, TypeError):  # a session from before the view-user-profile scope
        return {"characters": [], "relogin": True}
    chars = [{"name": c["name"], "server": c["server"]["name"], "region": c["server"]["region"]["slug"].lower(),
              "level": c.get("level") or 0, "class": CLASS_NAMES.get(c.get("classID"))}
             for c in (user or {}).get("characters") or [] if c.get("server")]
    out = {"characters": sorted(chars, key=lambda c: (-c["level"], c["name"]))}
    w.client.set_cached(MY_CHARACTERS_SAVED, out)  # served when Warcraft Logs can't answer later
    return out


@app.get("/api/realms")
def realms():
    """Realm names for the Server autocomplete, from the hand-kept data/realms.json (no API calls)."""
    data = json.loads((PROJECT_ROOT / "data" / "realms.json").read_text(encoding="utf-8"))
    return {r: sorted(set(names)) for r, names in data.items() if not r.startswith("_")}


# ---------- failures: every one is logged with context (Render logs) and answered with a friendly message ----------

@app.middleware("http")
async def request_id(request: Request, call_next):
    """A short id per request, sent back as X-Request-Id and shown small in the page's error banner, so a user's
    report can be matched to the log line."""
    request.state.rid = uuid.uuid4().hex[:8]
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.rid
    return response


def _rid(request: Request) -> str:
    return getattr(request.state, "rid", "-")


def _fail(request: Request, status: int, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"detail": detail, "ref": _rid(request), **extra}, status_code=status,
                        headers={**(headers or {}), "X-Request-Id": _rid(request)})


@app.exception_handler(WCLError)
def wcl_error(request: Request, e: WCLError):
    """A Warcraft Logs refusal is never a 500: the page shows the message with a "Try again"."""
    log.warning("WCL error on %s %s (ref %s, rate_limited=%s): %s", request.method, request.url.path, _rid(request),
                e.rate_limited, e)
    headers = {"Retry-After": str(round(e.retry_after))} if e.retry_after else {}
    return _fail(request, 429 if e.rate_limited else 503, str(e), headers, rate_limited=e.rate_limited)


@app.exception_handler(httpx.HTTPError)
def http_error(request: Request, e: httpx.HTTPError):
    """Network trouble or an error status from Warcraft Logs (token or API): a message, not a 500."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    log.warning("WCL HTTP error on %s %s (ref %s): %s %s", request.method, request.url.path, _rid(request),
                type(e).__name__, status or "")
    return _fail(request, 429 if status == 429 else 503, "Warcraft Logs didn't answer properly"
                 + (f" ({status})" if status else "") + " - it may be busy. Try again in a minute.",
                 rate_limited=status == 429)


@app.exception_handler(Exception)
def unexpected_error(request: Request, e: Exception):
    """Anything else: the traceback goes to the log, the page gets a plain message with the ref to report."""
    log.error("unhandled error on %s %s (ref %s)", request.method, request.url.path, _rid(request),
              exc_info=(type(e), e, e.__traceback__))
    return _fail(request, 500, "Something went wrong on our side. Try again - if it keeps happening, mention "
                               f"ref {_rid(request)} when you report it.")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    if AUTH_MODE != "wcl":
        return {"mode": "local", "user": None}
    s = auth.get_session(app_client, request.cookies.get(auth.SESSION_COOKIE))
    return {"mode": "wcl", "user": s.user_name if s else None, "user_id": s.user_id if s else None,
            "admin": bool(s and s.user_id in ADMIN_USER_IDS)}


@app.get("/login")
def login(popup: bool = False):
    """popup: the page opened sign-in in a small window; the callback then answers with a page that tells the opener
    and closes itself instead of redirecting."""
    url, state = auth.start_login(app_client)
    r = RedirectResponse(url, status_code=302)
    r.set_cookie(auth.STATE_COOKIE, state, max_age=auth.STATE_TTL_S, httponly=True, secure=SECURE_COOKIES, samesite="lax")
    if popup:
        r.set_cookie(POPUP_COOKIE, "1", max_age=auth.STATE_TTL_S, httponly=True, secure=SECURE_COOKIES, samesite="lax")
    return r


POPUP_COOKIE = "hr_signin_popup"
POPUP_DONE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Healer Review</title><style>
:root { --bg: #0f1218; --panel: #171b23; --text: #e6e9ef; --muted: #98a2b3; --good: #45d483; --bad: #f25c5c; }
@media (prefers-color-scheme: light) { :root { --bg: #f6f7f9; --panel: #fff; --text: #1a1f2b; --muted: #5b6475;
  --good: #15803d; --bad: #b91c1c; } }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text);
  font: 15px/1.5 Inter, system-ui, sans-serif; }
.card { background: var(--panel); border-radius: 14px; padding: 28px 32px; text-align: center; max-width: 320px; }
.mark { font-size: 28px; color: var(--%(tone)s); } p { color: var(--muted); margin: 6px 0 0; }
</style></head><body><div class="card"><div class="mark">%(mark)s</div><strong>%(title)s</strong><p>%(text)s</p></div>
<script>
try { window.opener && window.opener.postMessage(%(msg)s, location.origin); } catch (e) {}
setTimeout(() => window.close(), %(close_ms)d);
</script></body></html>"""


def _popup_done(ok: bool, reason: str = "") -> HTMLResponse:
    page = POPUP_DONE % {"tone": "good" if ok else "bad", "mark": "&#10003;" if ok else "!",
                         "title": "Signed in" if ok else "Sign-in didn't finish",
                         "text": "Taking you back to Healer Review..." if ok else "Close this window and try again.",
                         "msg": json.dumps({"type": "hr-signin", "ok": ok, "reason": reason}), "close_ms": 700 if ok else 2500}
    r = HTMLResponse(page)
    r.delete_cookie(POPUP_COOKIE)
    return r


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    popup = request.cookies.get(POPUP_COOKIE) == "1"
    if error or not code:
        return _popup_done(False, "cancelled") if popup else RedirectResponse("/?signin=cancelled", status_code=302)
    try:
        s = auth.finish_login(app_client, code, state, request.cookies.get(auth.STATE_COOKIE))
    except PermissionError as e:  # state mismatch / expired
        log.warning("sign-in failed: %s", e)
        return _popup_done(False, "state") if popup else RedirectResponse("/?signin=failed&reason=state", status_code=302)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:  # WCL busy / rate limited / rejected the code
        reason, detail = _signin_reason(e)
        log.warning("sign-in failed: %s (%s)", reason, detail)  # status + WCL's error field only, never the code/token
        return _popup_done(False, reason) if popup else RedirectResponse(f"/?signin=failed&reason={reason}", status_code=302)
    r = _popup_done(True) if popup else RedirectResponse("/", status_code=302)
    r.delete_cookie(auth.STATE_COOKIE)
    r.set_cookie(auth.SESSION_COOKIE, s.id, max_age=int(s.expires - datetime.now().timestamp()), httponly=True,
                 secure=SECURE_COOKIES, samesite="lax")
    return r


def _signin_reason(e: Exception) -> tuple[str, str]:
    """(short reason code for the page, detail for the server log) - no secrets in either."""
    if isinstance(e, httpx.TimeoutException):
        return "timeout", type(e).__name__
    resp = getattr(e, "response", None)
    if resp is None:
        return "wcl_error", type(e).__name__
    try:
        body = resp.json()
        err, desc = str(body.get("error", "")), str(body.get("error_description", "") or body.get("message", ""))
    except ValueError:
        err, desc = "", ""
    detail = f"HTTP {resp.status_code} {err} {desc[:120]}".strip()
    if resp.status_code == 429:
        return "rate_limited", detail
    if "redirect" in (err + desc).lower():
        return "redirect_mismatch", detail
    if err in ("invalid_grant", "invalid_client", "invalid_request", "unauthorized_client"):
        return err, detail
    return f"http_{resp.status_code}", detail


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
    saved = {"name": char["name"], "server": char["server"]["name"], "region": s.region.lower(),
             "class": CLASS_NAMES.get(char.get("classID"))}
    w.settings_path.parent.mkdir(parents=True, exist_ok=True)
    w.settings_path.write_text(json.dumps(saved))
    return saved


@app.get("/api/recent")
def recent(limit: int = RECENT_FIRST, fresh: bool = False, saved: bool = False, w: Who = Depends(who)):
    """Your recent keys from the newest `limit` reports (the page asks for more with "Load more"). Reports older
    than a day can't change, so their fight lists come from the cache; on a WCL rate limit it returns what it has
    found so far with a note instead of failing. fresh: the refresh button - skip the 5-minute cache of new reports.
    saved: the last list built, without any Warcraft Logs call - the page shows it at once, then asks for the live one."""
    s, client = load_settings(w), w.client.quick()
    if not s:
        return {"runs": []}
    ttl = 1 if fresh else RECENT_TTL_S
    saved_key = f"recent_saved:{s['name'].lower()}:{server_slug(s['server'])}:{s['region']}"
    if saved:
        out = client.get_cached(saved_key) or {"runs": [], "more": False}
        for r in out["runs"]:  # analyzed / queued may have changed since the list was saved: from our own cache
            code, fid = parse_report_url(r["url"])
            r["analyzed"] = client.get_cached(latest_key(code, int(fid), s["name"], TOP_LOGS)) is not None
            r["queued"] = not r["analyzed"] and bool(queue.pending(w.user_id, "analyze", r["url"]))
        return {**out, "saved": True, "partial": None}
    try:
        char = lookup_character(client, s, ttl=ttl)
    except WCLError as e:  # Warcraft Logs busy: the last list we built, with a quiet note, instead of an error
        saved = client.get_cached(saved_key)
        if not saved:
            raise
        log.warning("recent keys from saved list (user %s): %s", w.user_id, e)
        return {**saved, "note": "Showing your saved list - Warcraft Logs is busy right now; it refreshes "
                                 "automatically.", "partial": None}
    if not char:
        raise HTTPException(404, "Character not found on Warcraft Logs.")
    if not s.get("class") and CLASS_NAMES.get(char.get("classID")):  # saved before the class was kept: fill it in
        w.settings_path.write_text(json.dumps({**s, "class": CLASS_NAMES[char["classID"]]}))
    # Warcraft Logs links reports to a character through rankings, so a session of only untimed keys never shows up
    # there. Reports you uploaded yourself are added too (hosted: your sign-in; locally: WCL_USER_ID).
    reports = {r["code"]: r for r in char["recentReports"]["data"]}
    uploader = w.user_id or LOCAL_WCL_USER_ID
    if uploader:
        try:
            mine = client.query(UPLOADS_QUERY, {"uid": uploader, "limit": RECENT_REPORTS}, ttl=ttl)
            for r in mine["reportData"]["reports"]["data"]:
                reports.setdefault(r["code"], r)
        except (WCLError, httpx.HTTPError, KeyError, TypeError) as e:
            log.warning("own uploads unavailable (user %s): %s", uploader, e)
    reports = sorted(reports.values(), key=lambda r: -r["startTime"])
    runs, seen, partial = [], [], None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    for rep in reports[:max(1, limit)]:
        try:
            runs += _report_runs(client, rep, s, seen, None if now_ms - rep["startTime"] > FINISHED_REPORT_MS else ttl)
        except WCLError as e:
            log.warning("recent keys partial (user %s, report %s): %s", w.user_id, rep["code"], e)
            partial = str(e)
            break
    runs.sort(key=lambda r: r["started"], reverse=True)
    out = {"character": f"{char['name']} - {char['server']['name']}", "runs": runs, "partial": partial,
           "more": limit < len(reports)}
    if not partial:
        client.set_cached(saved_key, out)  # shown when Warcraft Logs is busy next time
    _auto_analyze(w, runs, s)
    return out


def _auto_analyze(w: Who, runs: list[dict], s: dict) -> None:
    """Queue your newest keys that aren't analyzed yet (up to AUTO_ANALYZE), so they're ready when you click.
    Marks each queued run, so the list can say so."""
    for r in runs[:AUTO_ANALYZE]:
        abandoned = not r["timed"] and r["duration_min"] < ABANDONED_MIN  # not worth API points unless you click it
        if r["analyzable"] and not r["analyzed"] and not abandoned and not queue.pending(w.user_id, "analyze", r["url"]):
            queue.submit(w.user_id, "analyze", {"url": r["url"], "name": s["name"], "fresh": False, "auto": True})
    for r in runs:
        r["queued"] = not r["analyzed"] and bool(queue.pending(w.user_id, "analyze", r["url"]))


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
            "analyzed": client.get_cached(latest_key(rep["code"], f.id, s["name"], TOP_LOGS)) is not None,
        })
    return runs


@app.get("/api/budget")
def budget(w: Who = Depends(who)):
    r = w.client.quick().query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }", ttl=0)["rateLimitData"]
    return {"used": round(r["pointsSpentThisHour"]), "limit": r["limitPerHour"], "reset_in_s": r["pointsResetIn"]}


# ---------- visitor counts ----------
# Page views go to GoatCounter from the server, so ad blockers (which block GoatCounter's own script) don't hide
# visitors. The token lives only in the host's environment; without it nothing is sent (local runs).
GOATCOUNTER_TOKEN = os.environ.get("GOATCOUNTER_TOKEN", "")
GOATCOUNTER_URL = os.environ.get("GOATCOUNTER_URL", "https://rviens.goatcounter.com") + "/api/v0/count"


class PageView(BaseModel):
    path: str = "/"
    title: str = ""
    ref: str = ""


_views: "queue_mod.Queue[dict]" = queue_mod.Queue(maxsize=200)  # a burst beyond this is dropped, not queued forever


def _view_sender() -> None:
    while True:
        _send_view(_views.get())


def _send_view(hit: dict) -> None:
    try:
        r = httpx.post(GOATCOUNTER_URL, json={"no_sessions": False, "hits": [hit]}, timeout=10,
                       headers={"Authorization": f"Bearer {GOATCOUNTER_TOKEN}"})
        if r.status_code >= 400:
            log.warning("goatcounter: %s %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        log.warning("goatcounter: %s", e)


@app.post("/api/pv", status_code=204)
def page_view(pv: PageView, request: Request):
    ua = request.headers.get("user-agent", "")
    if not GOATCOUNTER_TOKEN or not ua or "bot" in ua.lower():
        return
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    hit = {"path": pv.path[:200] or "/", "title": pv.title[:200], "ref": pv.ref[:500], "ip": ip, "user_agent": ua}
    try:
        _views.put_nowait(hit)
    except queue_mod.Full:
        log.warning("goatcounter: dropping a page view (sender is behind)")


# ---------- analysis and comparison (background jobs, visible only to their owner) ----------

# Analyses run as queued jobs (app/jobs.py): they survive restarts and wait out Warcraft Logs rate limits.
queue = Jobs(app_client)
PUBLIC_REPORT_QUERY = "query($code: String!) { reportData { report(code: $code) { visibility } } }"


def client_for(owner: int) -> WCLClient:
    """The API client a queued job runs with: the owner's own sign-in when hosted."""
    if not owner:
        return app_client
    token = auth.token_for_user(app_client, owner)
    if not token:
        raise WCLError("Your Warcraft Logs sign-in has expired - sign in again to analyze new runs.")
    return app_client.for_user(token, owner)


def public_latest_key(code: str, fight_id: int) -> str:
    return f"public_latest:{code}:{fight_id}"


# ---------- top-player study, kept up to date on its own ----------
# Dungeons rotate every season: when a run is analyzed in a dungeon the spec's study doesn't cover yet, that dungeon
# is studied in the background (on the site's own API budget, which keeps a reserve; never while an analysis runs),
# so "Top players" fills in by itself. One at a time; a restart forgets the list, and the next analysis asks again.
AUTO_STUDY_RUNS = 5
_auto_study: list[tuple[str, str]] = []  # (spec key, dungeon)
_auto_lock = threading.Lock()
_auto_thread: threading.Thread | None = None


def _want_study(spec_key: str, dungeon: str) -> None:
    global _auto_thread
    spec = next((s for s in SPECS if s.key == spec_key), None)
    try:
        if not spec or covers(app_client, spec, dungeon):
            return
    except (WCLError, httpx.HTTPError, KeyError):  # can't tell which season it is right now: ask again next time
        return
    with _auto_lock:
        if (spec_key, dungeon) in _auto_study:
            return
        _auto_study.append((spec_key, dungeon))
        log.info("auto study queued: %s %s", spec.label, dungeon)
        if not (_auto_thread and _auto_thread.is_alive()):
            _auto_thread = threading.Thread(target=_auto_study_loop, daemon=True)
            _auto_thread.start()


def _auto_study_loop() -> None:
    while True:
        with _auto_lock:
            if not _auto_study:
                return
            spec_key, dungeon = _auto_study[0]
        while queue.busy() or study_job.get("status") == "running":  # people's analyses and the admin study first
            time.sleep(30)
        spec = next(s for s in SPECS if s.key == spec_key)
        try:
            if not covers(app_client, spec, dungeon):  # someone may have studied it meanwhile
                run_study(app_client, spec, AUTO_STUDY_RUNS, dungeon=dungeon, progress=lambda m: log.info("auto study: %s", m))
                add_curves(app_client, spec, progress=lambda m: log.info("auto study: %s", m))
        except Exception:  # noqa: BLE001 - a failed study must not stop the next one; the log has why
            log.exception("auto study failed: %s %s", spec_key, dungeon)
        finally:
            with _auto_lock:
                _auto_study.pop(0)


@queue.handler("analyze")
def _analyze_job(owner: int, p: dict, progress, preview) -> str:
    client = client_for(owner)
    out = run_analysis(client, p["url"], p.get("name"), TOP_LOGS, progress=progress, use_cache=not p.get("fresh"),
                       preview=preview)
    _want_study((out.get("spec") or {}).get("key", ""), out["run"]["dungeon"])
    if owner:  # hosted: a public report's analysis can also be opened by anyone with the link, signed out
        try:
            if client.query(PUBLIC_REPORT_QUERY, {"code": out["key"].split(":")[2]})["reportData"]["report"]["visibility"] == "public":
                pub = app_client.public()
                pub.set_cached(out["key"], out)
                pub.set_cached(public_latest_key(*out["key"].split(":")[2:4]), out["key"])
        except (WCLError, httpx.HTTPError, KeyError, TypeError):
            log.warning("could not check report visibility for %s; kept private", out["key"].split(":")[2])
    return out["key"]


# The cache lives on a small disk (1 GB on Render): hourly, the oldest raw downloads go once it passes the cap.
CACHE_MAX_MB = int(os.environ.get("CACHE_MAX_MB", "700"))
CACHE_TARGET_MB = int(os.environ.get("CACHE_TARGET_MB", "500"))


def _prune_loop() -> None:
    while True:
        try:
            n = app_client.prune(CACHE_MAX_MB * 2**20, CACHE_TARGET_MB * 2**20)
            if n:
                log.info("cache: pruned %d old downloads, now %d MB", n, app_client.used_bytes() // 2**20)
        except Exception:  # noqa: BLE001 - never let housekeeping take the site down
            log.exception("cache prune failed")
        time.sleep(3600)


@app.on_event("startup")
def _start_queue():
    queue.start()
    threading.Thread(target=_view_sender, daemon=True).start()
    threading.Thread(target=_prune_loop, daemon=True).start()
    FACTS.refresh_if_stale()  # game spell data + community avoidable list, weekly, in the background


@app.post("/api/analyze")
def start_analysis(req: AnalyzeRequest, w: Who = Depends(who)):
    """A stored analysis on the current rules opens at once (no Warcraft Logs calls). Otherwise a job is queued -
    and the newest stored version, if any, is returned with it to show while it updates."""
    try:
        code, fid = parse_report_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = (load_settings(w) or {}).get("name")
    stored = stored_analysis(w.client, code, fid, name, TOP_LOGS) if fid is not None and name and not req.fresh else None
    if stored and _outdated(w.client, code, fid, stored):
        log.info("stored analysis of %s#%s no longer matches the report - redoing it", code, fid)
        w.client.forget_fight(code, int(fid))
        app_client.public().forget_fight(code, int(fid))
        stored = None
    if stored and stored.get("rules_version") == RULES_VERSION:
        return {"result": stored}
    job_id = None if req.fresh else queue.pending(w.user_id, "analyze", req.url)
    if job_id:
        queue.prioritize(job_id)  # already queued (maybe in the background): reuse it, now as a click
    else:
        job_id = queue.submit(w.user_id, "analyze", {"url": req.url, "name": name, "fresh": req.fresh})
    return {"job": job_id, "result": stored, "stale": bool(stored)}


def _outdated(client: WCLClient, code: str, fid: str, stored: dict) -> bool:
    """A key analyzed while it was still live-logging can be a different key by now (Warcraft Logs renumbers or
    extends fights): its saved level or length no longer match the report's fight list. That list is the one the
    key list already cached, so this costs no extra request."""
    try:
        fight = pick_fight(get_fights(client.quick(), code), fid)
    except (WCLError, httpx.HTTPError, ValueError, StopIteration):
        return False  # can't tell: keep what we have
    run = stored.get("run") or {}
    return (run.get("level") != (fight.keystone_level or 0)
            or abs((run.get("duration_min") or 0) - fight.duration_s / 60) > 1)


_io_running: set[str] = set()


@app.get("/api/io")
def raiderio_scores(url: str, w: Who = Depends(who)):
    """Average Raider.io score of your group and of each compared top group (players' CURRENT season scores).
    Answers from the cache at once; missing scores are fetched in the background - the page asks again while
    "pending". Never fails the page: on any trouble it just has fewer numbers."""
    try:
        code, fid = parse_report_url(url)
        fight = pick_fight(get_fights(w.client, code), fid)
        mine = player_realms(w.client, fight)
        stored = stored_analysis(w.client, code, fight.id, (load_settings(w) or {}).get("name") or "", TOP_LOGS) or {}
        pub = app_client.public()
        tops = []
        for lg in (stored.get("compared") or {}).get("logs", []):
            f = next((x for x in get_fights(pub, lg["code"]) if x.id == lg["fight_id"]), None)
            if f:
                tops.append((lg, player_realms(pub, f)))
        # Top players: the season study's runs of this dungeon (cached lookups - the study fetched them)
        study = []
        spec = next((s for s in SPECS if s.key == (stored.get("spec") or {}).get("key")), None)
        model = load_dungeon(fight.name, spec) if spec else None
        for sr in (model.data.get("runs", []) if model else []):
            f = next((x for x in get_fights(pub, sr["code"]) if x.id == sr["fight"]), None)
            if f:
                study.append(({"player": sr["player"], "key": sr["key"]}, player_realms(pub, f)))
    except (WCLError, httpx.HTTPError, ValueError, KeyError, StopIteration) as e:
        log.warning("io scores unavailable for %s: %s", url, e)
        return {"group": None, "tops": [], "pending": False}
    group = raiderio.group_scores(pub, mine, fetch=False)
    top_rows = [{**lg, **raiderio.group_scores(pub, ps, fetch=False)} for lg, ps in tops]
    study_rows = [{**lg, **raiderio.group_scores(pub, ps, fetch=False)} for lg, ps in study]
    pending = bool(group["pending"] or any(t["pending"] for t in top_rows + study_rows))
    if pending and url not in _io_running:  # fill the cache in the background, one request at a time
        _io_running.add(url)

        def fill():
            try:
                for ps in [mine] + [ps for _, ps in tops] + [ps for _, ps in study]:
                    raiderio.group_scores(pub, ps, fetch=True)
            finally:
                _io_running.discard(url)
        threading.Thread(target=fill, daemon=True).start()

    def mean(rows):
        avgs = [t["avg"] for t in rows if t["avg"]]
        return round(sum(avgs) / len(avgs)) if avgs else None
    top_avg, study_avg = mean(top_rows), mean(study_rows)
    strip = lambda rows: [{k: v for k, v in t.items() if k != "players"} for t in rows]
    return {"group": {**group, "color": raiderio.score_color(pub, group["avg"])}, "tops": strip(top_rows),
            "top_avg": top_avg, "top_color": raiderio.score_color(pub, top_avg), "study": strip(study_rows),
            "study_avg": study_avg, "study_color": raiderio.score_color(pub, study_avg), "pending": pending}


@app.get("/api/best")
def best_key(url: str, w: Who = Depends(who)):
    """Your best timed key level this season (Raider.io), for how strict the review is on this run."""
    try:
        code, fid = parse_report_url(url)
        fight = pick_fight(get_fights(w.client, code), fid)
        name = ((load_settings(w) or {}).get("name") or "").lower()
        players = player_realms(w.client, fight)
        me = next((p for p in players if p["name"].lower() == name), None)
        if not me:  # not your run: the healer it was analyzed for
            stored = stored_analysis(w.client, code, fight.id, name, TOP_LOGS) or {}
            me = next((p for p in players if p["name"] == (stored.get("run") or {}).get("player")), None)
        best = raiderio.best_timed(app_client.public(), me["region"], me["realm"], me["name"]) if me else None
        return {"best": best, "player": me["name"] if me else None}
    except (WCLError, httpx.HTTPError, ValueError, KeyError, StopIteration):
        return {"best": None, "player": None}


@app.get("/api/public-analysis")
def public_analysis(url: str):
    """Signed out: a saved analysis of a public run, from a link. Analyzing new runs needs a sign-in."""
    try:
        code, fid = parse_report_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pub = app_client.public()
    key = pub.get_cached(public_latest_key(code, fid)) if fid is not None else None
    result = pub.get_cached(key) if key else None
    if not result:
        raise HTTPException(404, "No saved analysis of this run yet - sign in to analyze it.")
    return {"result": result}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, skip_preview: bool = False, w: Who = Depends(who)):
    job = queue.get(job_id)
    if not job or job["owner"] != w.user_id:
        raise HTTPException(404, "Unknown job")
    out = {k: job[k] for k in ("status", "progress", "note", "error", "position", "attempts", "preview")}
    out["ref"] = job_id[:8]
    if skip_preview:  # the page already shows it; it's large
        out["preview"] = None
    if job["status"] == "done":
        out["result"] = w.client.get_cached(job["result_key"])
    return out

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
    """Study status for the button; can_run only for admins (checked again on every admin endpoint)."""
    covered = [{"key": sp.key, "label": sp.label, "generated": i.get("generated")}
               for sp in SPECS if (i := study_info(sp))["exists"]]
    return {**study_info(_spec(spec)), "job": study_job, "can_run": w.is_admin, "covered": covered,
            "specs": [{"key": sp.key, "label": sp.label} for sp in SPECS]}


@app.post("/api/study")
def start_study(req: StudyRequest, w: Who = Depends(who)):
    if not w.is_admin:  # one shared study per spec: only the site admins refresh it
        log.warning("study refresh refused: user %s is not an admin", w.user_id)
        raise HTTPException(403, "Only the site admin can refresh the top-player study.")
    spec = _spec(req.spec)
    log.info("admin action: user %s started the %s study (%d runs/dungeon)", w.user_id, spec.key, req.runs)
    if study_job.get("status") == "running":
        raise HTTPException(409, "A study is already running.")
    study_job.clear()
    study_job.update({"status": "running", "progress": [], "error": None})

    def work():
        try:
            run_study(app_client, spec, max(3, min(req.runs, 20)), progress=study_job["progress"].append)
            add_curves(app_client, spec, progress=study_job["progress"].append)  # top-player HP bands (cached runs)
            study_job["status"] = "done"
            log.info("study done: %s", spec.key)
        except Exception as e:  # report to the page; cached runs make a retry cheap
            log.exception("study failed (%s)", spec.key)
            study_job["status"], study_job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return study_job


@app.get("/api/study/export")
def export_study(spec: str, w: Who = Depends(who)):
    """Admins: download a study file, to commit it to the repo (data/context_models/<spec>.json)."""
    if not w.is_admin:
        raise HTTPException(403, "Only the site admin can export the study.")
    path = model_path(_spec(spec))
    if not path.exists():
        raise HTTPException(404, "No study for that spec yet.")
    log.info("admin action: user %s exported the %s study", w.user_id, spec)
    return FileResponse(path, filename=f"{spec}.json", media_type="application/json")
