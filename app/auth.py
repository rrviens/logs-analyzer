"""Sign in with Warcraft Logs (hosted mode, AUTH_MODE=wcl).

OAuth 2 authorization-code flow with PKCE: /login sends the visitor to Warcraft Logs with a one-time `state`
(CSRF protection, also kept in a short-lived cookie) and a PKCE challenge; /callback checks the state,
exchanges the code for the visitor's own access token, and starts a server-side session. The browser only
gets a random session id (HttpOnly, SameSite=Lax, Secure over https) - the token never leaves the server.
"""
import base64
import hashlib
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from app.config import REDIRECT_URI, wcl_credentials
from app.wcl import TOKEN_URL, USER_API, WCLClient

AUTHORIZE_URL = "https://www.warcraftlogs.com/oauth/authorize"
STATE_TTL_S = 600
SESSION_COOKIE, STATE_COOKIE = "hr_session", "hr_oauth_state"
ME_QUERY = "{ userData { currentUser { id name } } }"


@dataclass
class Session:
    id: str
    user_id: int
    user_name: str
    token: str
    expires: float


def _tables(client: WCLClient) -> None:
    with client.lock:
        client.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id INTEGER, user_name TEXT, "
                          "token TEXT, expires REAL, created REAL)")
        client.db.execute("CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, verifier TEXT, created REAL)")
        client.db.commit()


def start_login(client: WCLClient) -> tuple[str, str]:
    """(authorize URL, state). The PKCE verifier stays server-side, keyed by the state."""
    _tables(client)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with client.lock:
        client.db.execute("DELETE FROM oauth_states WHERE created < ?", (time.time() - STATE_TTL_S,))
        client.db.execute("INSERT INTO oauth_states VALUES (?, ?, ?)", (state, verifier, time.time()))
        client.db.commit()
    # view-user-profile: lets the page list the visitor's claimed characters (character picker).
    params = {"client_id": wcl_credentials()[0], "redirect_uri": REDIRECT_URI, "response_type": "code",
              "scope": "view-user-profile", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state


def finish_login(client: WCLClient, code: str, state: str, cookie_state: str | None) -> Session:
    """Check the state (must match the cookie and be fresh, single use), swap the code for a token, open a session."""
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise PermissionError("Sign-in state mismatch - please try again.")
    with client.lock:
        row = client.db.execute("SELECT verifier, created FROM oauth_states WHERE state = ?", (state,)).fetchone()
        client.db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        client.db.commit()
    if not row or time.time() - row[1] > STATE_TTL_S:
        raise PermissionError("Sign-in expired - please try again.")
    r = httpx.post(TOKEN_URL, auth=wcl_credentials(), timeout=30,
                   data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                         "code_verifier": row[0]})
    r.raise_for_status()
    tok = r.json()
    me = httpx.post(USER_API, json={"query": ME_QUERY}, timeout=30,
                    headers={"Authorization": f"Bearer {tok['access_token']}"}).json()["data"]["userData"]["currentUser"]
    s = Session(secrets.token_urlsafe(32), int(me["id"]), me["name"], tok["access_token"],
                time.time() + tok.get("expires_in", 3600 * 24 * 30))
    with client.lock:
        client.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                          (s.id, s.user_id, s.user_name, s.token, s.expires, time.time()))
        client.db.commit()
    return s


def get_session(client: WCLClient, session_id: str | None) -> Session | None:
    if not session_id:
        return None
    _tables(client)
    with client.lock:
        row = client.db.execute("SELECT id, user_id, user_name, token, expires FROM sessions WHERE id = ?",
                                (session_id,)).fetchone()
    if not row or row[4] < time.time():
        return None
    return Session(*row)


def end_session(client: WCLClient, session_id: str | None) -> None:
    if session_id:
        with client.lock:
            client.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            client.db.commit()
