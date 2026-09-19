import os
from dotenv import load_dotenv
load_dotenv()

def env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        raise RuntimeError(f"Missing env var {name} (see .env.example)")
    return v

DATABASE_URL     = env("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/hamsa")
WA_ACCESS_TOKEN  = env("WA_ACCESS_TOKEN", "")
WA_PHONE_ID      = env("WA_PHONE_NUMBER_ID", "")
WA_APP_SECRET    = env("WA_APP_SECRET", "")
WA_VERIFY_TOKEN  = env("WA_VERIFY_TOKEN", "")
WA_GRAPH_VERSION = env("WA_GRAPH_VERSION", "v24.0")
IDENTITY_SECRET  = env("IDENTITY_SECRET", "dev-only-secret")
DRY_RUN          = env("DRY_RUN", "0") == "1"
FLORENCE         = env("FLORENCE", "1") == "1"       # Florence-2 reads photo text first; Tesseract is the fallback (extract.py)
FLORENCE_MODEL   = env("FLORENCE_MODEL", "florence-community/Florence-2-base-ft")   # microsoft/Florence-2-base-ft in the native transformers layout
GRAPH = f"https://graph.facebook.com/{WA_GRAPH_VERSION}"
LOOKUP_COOLDOWN_S = 10          # 1 verdict lookup / 10 s / number
DAILY_REPORT_CAP  = 5           # 5 reports / day / identity
