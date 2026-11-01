"""Raider.io Mythic+ scores for the players in a log - the group's average next to the run, and the top groups'.

The public character endpoint gives the player's CURRENT season score, not their score at the time of the run - the
page says so. Polite by design: one request at a time with a short pause, a descriptive User-Agent, every answer
(found or not) cached in the shared cache for a day, and it stops at the first refusal (rate limit / outage). It
never raises: missing players or a Raider.io outage only make the numbers partial or absent.
"""
import logging
import os
import threading
import time
import unicodedata

import httpx

from app.wcl import WCLClient

log = logging.getLogger("uvicorn.error")
PROFILE_URL = "https://raider.io/api/v1/characters/profile"
USER_AGENT = "HealerReview/1.0 (+https://github.com/rrviens/logs-analyzer; M+ healer log analysis)"
CACHE_S = 24 * 3600
MISS_CACHE_S = 12 * 3600  # "not found" answers are retried sooner (renames, transfers)
PAUSE_S = 0.25  # between requests
API_KEY = os.environ.get("RAIDERIO_API_KEY", "")  # optional: Raider.io access key for higher limits

_lock = threading.Lock()  # one fetch at a time across all requests
_http = httpx.Client(timeout=6, headers={"User-Agent": USER_AGENT})
_blocked_until = [0.0]  # after a refusal, leave Raider.io alone for a while


def realm_slug(realm: str) -> str:
    """"Tarren Mill" -> "tarren-mill", "Aggra (Português)" -> "aggra-portugues"."""
    plain = unicodedata.normalize("NFKD", realm).encode("ascii", "ignore").decode().lower().strip()
    return "".join(ch for ch in plain.replace(" ", "-") if ch.isalnum() or ch == "-")


def _key(region: str, realm: str, name: str) -> str:
    return f"rio:v1:{region.lower()}:{realm.lower()}:{name.lower()}"


def cached_score(pub: WCLClient, region: str, realm: str, name: str) -> tuple[bool, float | None]:
    """(known, score) from the cache only."""
    hit = pub.get_cached(_key(region, realm, name))
    if not hit or time.time() - hit["t"] > (CACHE_S if hit["score"] is not None else MISS_CACHE_S):
        return False, None
    return True, hit["score"]


def fetch_score(pub: WCLClient, region: str, realm: str, name: str) -> float | None:
    """One character's current-season score (None if not found / no score). Cached; never raises."""
    known, score = cached_score(pub, region, realm, name)
    if known or time.time() < _blocked_until[0]:
        return score
    params = {"region": region.lower(), "realm": realm_slug(realm), "name": name,
              "fields": "mythic_plus_scores_by_season:current"}
    if API_KEY:
        params["access_key"] = API_KEY
    with _lock:
        time.sleep(PAUSE_S)
        try:
            r = _http.get(PROFILE_URL, params=params)
        except httpx.HTTPError as e:
            _blocked_until[0] = time.time() + 300
            log.warning("raider.io unreachable (%s) - pausing lookups for 5 min", type(e).__name__)
            return None
    if r.status_code in (429, 500, 502, 503, 504):
        _blocked_until[0] = time.time() + (float(r.headers.get("Retry-After") or 0) or 300)
        log.warning("raider.io answered %s - pausing lookups", r.status_code)
        return None
    score = None
    if r.status_code == 200:
        seasons = r.json().get("mythic_plus_scores_by_season") or []
        score = (seasons[0].get("scores") or {}).get("all") if seasons else None
        score = round(score) if score else None
    pub.set_cached(_key(region, realm, name), {"t": time.time(), "score": score})  # 400 = not found: cached too
    return score


def group_scores(pub: WCLClient, players: list[dict], fetch: bool) -> dict:
    """players: [{name, realm, region, class}]. Average over the players with a score; fetch=False reads the cache only."""
    rows = []
    for p in players:
        if fetch:
            score = fetch_score(pub, p["region"], p["realm"], p["name"])
            known = True
        else:
            known, score = cached_score(pub, p["region"], p["realm"], p["name"])
        rows.append({"name": p["name"], "class": p.get("class"), "score": score, "known": known})
    scores = [r["score"] for r in rows if r["score"]]
    return {"avg": round(sum(scores) / len(scores)) if scores else None, "with_score": len(scores), "total": len(rows),
            "pending": sum(1 for r in rows if not r["known"]), "players": rows}


