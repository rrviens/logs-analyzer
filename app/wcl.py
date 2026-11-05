"""Warcraft Logs API v2 client with a SQLite response cache.

Two kinds of client share one cache file:
- the app client (client credentials) - local single-user mode, the top-player study, and public data;
- a user client (a signed-in visitor's own OAuth token) - its queries go to the user endpoint, run on that
  visitor's own rate limit, and are cached under the visitor's namespace, so private reports never leak
  to anyone else. `client.public()` is the same token with the shared namespace: only for public data
  (ranked top logs), which reads the same for everyone and is fetched once for all users.

Safe to share between threads (the web app runs analyses in the background): cache access and token
refresh are serialized; HTTP calls run in parallel.
"""
import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
CLIENT_API = "https://www.warcraftlogs.com/api/v2/client"
USER_API = "https://www.warcraftlogs.com/api/v2/user"
RATE_QUERY = "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
RETRIES = 5  # on 429 (burst limit) / 5xx: back off 2, 4, 8, 16, 30s (or Retry-After)
QUICK_MAX_WAIT_S = 5.0  # quick() clients: one retry after at most this


AUX_WORKERS = 4  # small per-log / per-death fetches run this many at a time


class WCLError(RuntimeError):
    def __init__(self, message, rate_limited: bool = False, retry_after: float | None = None, uploading: bool = False):
        super().__init__(message)
        self.rate_limited, self.retry_after = rate_limited, retry_after
        self.uploading = uploading  # the log isn't fully processed yet: retried like a rate limit, own wording


def _retry_after(r: httpx.Response) -> float | None:
    try:
        value = float(r.headers.get("Retry-After") or 0)
        return value if value > 0 else None  # WCL sometimes sends 0 or a negative value
    except ValueError:  # an HTTP date instead of seconds
        return None


def parallel_map(fn, items, workers: int = AUX_WORKERS) -> list:
    """fn over items with overlapping network waits; results in input order."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


# Cache kinds that are plain downloads from Warcraft Logs (fetched again when missing). Query-cache rows are
# 64-char hashes. Everything else (analyses, "latest" pointers, settings, saved lists) is never pruned.
RAW_KINDS = {"run", "light", "dmgdone", "groupcasts", "discovery", "discovery-hots", "spellheals", "healerstate",
             "kicks", "auras", "defspans", "deathevents", "bosscasts", "bosshp", "avoidance"}


def _is_raw(key: str) -> bool:
    kind = re.sub(r"^u\d+:", "", key).split(":")[0]
    return kind in RAW_KINDS or bool(re.fullmatch(r"[0-9a-f]{64}", kind))


class WCLClient:
    def __init__(self, client_id: str, client_secret: str, cache_path: Path):
        self._auth = (client_id, client_secret)
        self._token: str | None = None
        self._token_expires = 0.0
        self._user_token: str | None = None  # set on user clients: never refreshed with client credentials
        self._ns = ""  # cache namespace: "" = shared, "u<id>:" = one user's
        self._api = CLIENT_API
        self._retries, self._max_wait = RETRIES, 60.0
        self._http = httpx.Client(timeout=60)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, created REAL, body TEXT)"
        )

    def for_user(self, token: str, user_id: int) -> "WCLClient":
        """A client acting as a signed-in visitor (same cache file, the visitor's own namespace and rate limit)."""
        c = copy.copy(self)
        c._user_token, c._ns, c._api = token, f"u{user_id}:", USER_API
        return c

    def public(self) -> "WCLClient":
        """The same credentials, shared cache namespace - only for public data such as ranked top logs."""
        c = copy.copy(self)
        c._ns = ""
        return c

    def quick(self) -> "WCLClient":
        """For requests a page is waiting on: one short retry on a rate limit instead of minutes of backoff
        (background analyses keep the patient client)."""
        c = copy.copy(self)
        c._retries, c._max_wait = 1, QUICK_MAX_WAIT_S
        return c

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def _get_token(self) -> str:
        if self._user_token:
            return self._user_token
        with self._lock:
            if self._token and time.time() < self._token_expires - 60:
                return self._token
            r = self._http.post(TOKEN_URL, auth=self._auth, data={"grant_type": "client_credentials"})
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 3600)
            return self._token

    def _read(self, key: str) -> tuple[float, str] | None:
        with self._lock:
            return self._db.execute("SELECT created, body FROM cache WHERE key = ?", (self._ns + key,)).fetchone()

    def _write(self, key: str, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, created, body) VALUES (?, ?, ?)", (self._ns + key, time.time(), body)
            )
            self._db.commit()

    def get_cached(self, key: str):
        row = self._read(key)
        return json.loads(row[1]) if row else None

    def set_cached(self, key: str, value) -> None:
        self._write(key, json.dumps(value))

    def forget_fight(self, code: str, fight_id: int) -> int:
        """Drops everything cached for one fight (run data, casts, auras, stored analyses) - for a copy fetched
        while the log was still uploading."""
        mine = "" if self._ns else " AND key NOT GLOB 'u[0-9]*:*'"  # shared: leave each user's own copies alone
        with self._lock:
            n = self._db.execute(f"DELETE FROM cache WHERE (key LIKE ? OR key LIKE ?){mine}",
                                 (f"{self._ns}%:{code}:{fight_id}:%", f"{self._ns}%:{code}:{fight_id}")).rowcount
            self._db.commit()
        return n

    def used_bytes(self) -> int:
        """Space the cache's rows take up (freed pages are reused by SQLite, so this is what keeps growing)."""
        page_count, free, size = (self._db.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "freelist_count", "page_size"))
        return (page_count - free) * size

    def prune(self, max_bytes: int, target_bytes: int) -> int:
        """Over max_bytes: deletes the oldest raw downloads (any user's) until under target_bytes. Only kinds that
        can be fetched again from Warcraft Logs go; saved analyses, settings and anything unknown stay."""
        if self.used_bytes() <= max_bytes:
            return 0
        with self._lock:
            keys = [k for (k,) in self._db.execute("SELECT key FROM cache ORDER BY created")]
        old = [k for k in keys if _is_raw(k)]
        removed = 0
        for i in range(0, len(old), 200):
            if self.used_bytes() <= target_bytes:
                break
            with self._lock:
                removed += self._db.executemany("DELETE FROM cache WHERE key = ?", [(k,) for k in old[i:i + 200]]).rowcount
                self._db.commit()
        return removed

    def _post(self, query: str, variables: dict | None) -> httpx.Response:
        """POST with backoff on 429 / 5xx. A 429 with the hourly points used up is not worth waiting out:
        it raises a clear message with the reset time (everything fetched so far is already cached)."""
        retries = self._retries
        for attempt in range(retries + 1):
            try:
                r = self._http.post(self._api, json={"query": query, "variables": variables or {}},
                                    headers={"Authorization": f"Bearer {self._get_token()}"})
            except httpx.TransportError:
                if attempt == retries:
                    raise
                time.sleep(max(0.5, min(2 ** attempt, self._max_wait)))
                continue
            if r.status_code != 429 and r.status_code < 500:
                return r
            retry_after = _retry_after(r)
            if r.status_code == 429:
                limit = self._rate_limit()
                if limit and limit["pointsSpentThisHour"] >= limit["limitPerHour"] - 1:
                    raise WCLError(f"Your Warcraft Logs API limit for this hour is used up "
                                   f"({limit['pointsSpentThisHour']:.0f}/{limit['limitPerHour']}); it resets in "
                                   f"{max(1, round(limit['pointsResetIn'] / 60))} min. Everything fetched so far is "
                                   f"saved - analyze again then and it continues where it stopped.",
                                   rate_limited=True, retry_after=limit["pointsResetIn"])
            if attempt == retries:
                if r.status_code == 429:
                    raise WCLError("Warcraft Logs is limiting requests right now - either too many in a short time "
                                   "or this hour's API limit is used up. Everything fetched so far is saved - try "
                                   "again in a few minutes (or after the hourly reset).",
                                   rate_limited=True, retry_after=retry_after)
                return r
            wait = retry_after or min(30.0, 2.0 * 2 ** attempt)
            time.sleep(max(0.5, min(wait, self._max_wait)))
        return r

    def _rate_limit(self) -> dict | None:
        try:
            r = self._http.post(self._api, json={"query": RATE_QUERY},
                                headers={"Authorization": f"Bearer {self._get_token()}"}, timeout=15)
            return r.json()["data"]["rateLimitData"] if r.status_code == 200 else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None

    def query(self, query: str, variables: dict | None = None, ttl: float | None = None) -> dict:
        """Run a GraphQL query. ttl=None caches forever (report data never changes);
        ttl=0 skips the cache."""
        key = hashlib.sha256(
            json.dumps({"q": query, "v": variables}, sort_keys=True).encode()
        ).hexdigest()

        if ttl != 0:
            row = self._read(key)
            if row and (ttl is None or time.time() - row[0] < ttl):
                return json.loads(row[1])

        r = self._post(query, variables)
        if r.status_code == 401 and self._user_token:
            raise WCLError("Your Warcraft Logs sign-in has expired - sign in again.")
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise WCLError(body["errors"])

        data = body["data"]
        if ttl != 0:
            self._write(key, json.dumps(data))
        return data
