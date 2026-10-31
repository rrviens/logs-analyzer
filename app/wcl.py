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


AUX_WORKERS = 4  # small per-log / per-death fetches run this many at a time


class WCLError(RuntimeError):
    pass


def parallel_map(fn, items, workers: int = AUX_WORKERS) -> list:
    """fn over items with overlapping network waits; results in input order."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


class WCLClient:
    def __init__(self, client_id: str, client_secret: str, cache_path: Path):
        self._auth = (client_id, client_secret)
        self._token: str | None = None
        self._token_expires = 0.0
        self._user_token: str | None = None  # set on user clients: never refreshed with client credentials
        self._ns = ""  # cache namespace: "" = shared, "u<id>:" = one user's
        self._api = CLIENT_API
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

    def _post(self, query: str, variables: dict | None) -> httpx.Response:
        """POST with backoff on 429 / 5xx. A 429 with the hourly points used up is not worth waiting out:
        it raises a clear message with the reset time (everything fetched so far is already cached)."""
        for attempt in range(RETRIES + 1):
            try:
                r = self._http.post(self._api, json={"query": query, "variables": variables or {}},
                                    headers={"Authorization": f"Bearer {self._get_token()}"})
            except httpx.TransportError:
                if attempt == RETRIES:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 429 and r.status_code < 500:
                return r
            if r.status_code == 429:
                limit = self._rate_limit()
                if limit and limit["pointsSpentThisHour"] >= limit["limitPerHour"] - 1:
                    raise WCLError(f"Your Warcraft Logs API limit for this hour is used up "
                                   f"({limit['pointsSpentThisHour']:.0f}/{limit['limitPerHour']}); it resets in "
                                   f"{max(1, round(limit['pointsResetIn'] / 60))} min. Everything fetched so far is "
                                   f"saved - analyze again then and it continues where it stopped.")
            if attempt == RETRIES:
                if r.status_code == 429:
                    raise WCLError("Warcraft Logs is limiting requests right now - either too many in a short time "
                                   "or this hour's API limit is used up. Everything fetched so far is saved - try "
                                   "again in a few minutes (or after the hourly reset).")
                return r
            wait = float(r.headers.get("Retry-After") or 0) or min(30.0, 2.0 * 2 ** attempt)
            time.sleep(min(wait, 60.0))
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
