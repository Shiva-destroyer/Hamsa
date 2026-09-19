"""Regulator demo: resolve a live dispute and show the verdict flip.

  python api/scripts/regulator_demo.py --batch AX2291 --outcome overturned    # lookup RED -> resolve -> lookup GREEN + history
  python api/scripts/regulator_demo.py --reset                                # re-run demo_fixups.sql: AX2291 back to RED, no dispute

Prereq: the API is running and a dispute for the batch is open (demo beat 5). Base URL: --base-url, else PUBLIC_BASE_URL,
else http://localhost:8000. Credentials: REGULATOR_USERNAME / REGULATOR_SHARED_SECRET (env or api/.env). Secrets and
tokens are never printed. Regulator API contract: POST /api/regulator/login, GET /api/regulator/disputes?status=,
POST /api/regulator/disputes/{id}/resolve {resolution, notes}.
"""
import argparse
import os
import sys
from pathlib import Path

import httpx

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))
FIXUPS = API_DIR.parent / "supabase" / "demo_fixups.sql"
DEFAULT_NOTES = "Regulator demo: manufacturer evidence accepted."


def lookup(client: httpx.Client, base: str, batch: str) -> str:
    r = client.get(f"{base}/api/lookup-batch/{batch}")
    r.raise_for_status()
    return r.json()["verdict"]


def login(client: httpx.Client, base: str, username: str, secret: str) -> dict:
    r = client.post(f"{base}/api/regulator/login", json={"username": username, "secret": secret})
    if r.status_code != 200:
        raise SystemExit(f"Regulator login failed (HTTP {r.status_code}). Check REGULATOR_USERNAME / REGULATOR_SHARED_SECRET / JWT_SECRET.")
    body = r.json()
    token = body.get("access_token") or body.get("token")
    if not token:
        raise SystemExit("Regulator login returned no token.")
    return {"Authorization": f"Bearer {token}"}


def find_dispute(client: httpx.Client, base: str, headers: dict, batch: str) -> dict | None:
    for status in ("open", "under_review"):
        r = client.get(f"{base}/api/regulator/disputes", params={"status": status}, headers=headers)
        r.raise_for_status()
        body = r.json()
        rows = body if isinstance(body, list) else body.get("disputes", body.get("items", []))
        for d in rows:
            if d.get("batch_number") == batch:
                return d
    return None


def resolve(client: httpx.Client, base: str, headers: dict, dispute_id: str, outcome: str, notes: str) -> None:
    r = client.post(f"{base}/api/regulator/disputes/{dispute_id}/resolve", json={"resolution": outcome, "notes": notes}, headers=headers)
    if r.status_code >= 400:
        raise SystemExit(f"Resolve failed (HTTP {r.status_code}): {r.text[:200]}")


def reset() -> None:
    """Re-run demo_fixups.sql (idempotent): AX2291 has no dispute and an active NSQ row again -> RED."""
    import psycopg
    import config
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        c.execute(FIXUPS.read_text(encoding="utf-8"))


def run(client: httpx.Client, base: str, batch: str, outcome: str, notes: str, username: str, secret: str, show_timeline=True) -> tuple[str, str]:
    before = lookup(client, base, batch)
    print(f"Lookup {batch} before: {before}")
    headers = login(client, base, username, secret)
    d = find_dispute(client, base, headers, batch)
    if not d:
        raise SystemExit(f"No open dispute for {batch}. File one first (WhatsApp: Dispute (makers) -> manufacturer email -> note).")
    print(f"Resolving dispute {str(d['id'])[:8]}... as {outcome}")
    resolve(client, base, headers, d["id"], outcome, notes)
    after = lookup(client, base, batch)
    print(f"Lookup {batch} after:  {after}   ({before} -> {after})")
    if show_timeline:
        import timeline
        print("\n" + timeline.format_timeline(batch, "en"))
    return before, after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", default="AX2291")
    ap.add_argument("--outcome", choices=["upheld", "overturned"], default="overturned")
    ap.add_argument("--notes", default=DEFAULT_NOTES)
    ap.add_argument("--base-url", help="default: PUBLIC_BASE_URL or http://localhost:8000")
    ap.add_argument("--reset", action="store_true", help="re-run demo_fixups.sql instead of resolving")
    a = ap.parse_args()
    import config  # noqa: F401  (loads api/.env into the environment)
    base = (a.base_url or os.getenv("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
    with httpx.Client(timeout=15) as client:
        if a.reset:
            reset()
            print("demo_fixups.sql applied.")
            try:
                print(f"Lookup {a.batch}: {lookup(client, base, a.batch)}")
            except httpx.HTTPError:
                print(f"(API at {base} not reachable; lookup skipped)")
            return 0
        user, secret = os.getenv("REGULATOR_USERNAME"), os.getenv("REGULATOR_SHARED_SECRET")
        if not user or not secret:
            sys.exit("Set REGULATOR_USERNAME and REGULATOR_SHARED_SECRET (env or api/.env).")
        try:
            run(client, base, a.batch, a.outcome, a.notes, user, secret)
        except httpx.HTTPError as e:
            sys.exit(f"API call failed: {type(e).__name__} (is the server running at {base}?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
