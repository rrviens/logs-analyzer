import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "cache.sqlite3"

load_dotenv(PROJECT_ROOT / ".env")


def wcl_credentials() -> tuple[str, str]:
    client_id = os.getenv("WCL_CLIENT_ID")
    client_secret = os.getenv("WCL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(f"WCL_CLIENT_ID / WCL_CLIENT_SECRET missing in {PROJECT_ROOT / '.env'}")
    return client_id, client_secret
