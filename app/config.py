"""Settings from environment variables (a local .env file is read too; never commit it).

WCL_CLIENT_ID / WCL_CLIENT_SECRET  your Warcraft Logs API client (https://www.warcraftlogs.com/api/clients)
AUTH_MODE      "local" (default): single user, everything runs on the app client - the start.bat setup.
               "wcl": hosted site - every visitor signs in with their own Warcraft Logs account.
REDIRECT_URI   where Warcraft Logs sends visitors back after sign-in; must be listed in the API client's
               redirect URLs. Default http://localhost:8000/callback.
DATA_DIR       where the cache, sessions and per-user settings live (a persistent disk when hosted).
               Default: the project folder.
SECURE_COOKIES "1" when served over https (the default in "wcl" mode); "0" for plain-http local testing.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or PROJECT_ROOT)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = DATA_DIR / "cache.sqlite3"
AUTH_MODE = os.getenv("AUTH_MODE", "local").lower()
# Warcraft Logs user ids (comma-separated) of the site admins: they can refresh the top-player study from the page.
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/callback")
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "1" if AUTH_MODE == "wcl" else "0") == "1"


def wcl_credentials() -> tuple[str, str]:
    client_id = os.getenv("WCL_CLIENT_ID")
    client_secret = os.getenv("WCL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("WCL_CLIENT_ID / WCL_CLIENT_SECRET are not set (environment or a local .env file)")
    return client_id, client_secret
