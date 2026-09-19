"""Build (or refresh) the Hamsa database. psycopg v3 only -- no psql binary needed; works on any OS.

  python api/scripts/db_setup.py --db hamsa --reset     # drop + create + full rebuild
  python api/scripts/db_setup.py --db hamsa             # create if missing + full build (fails if already built)
  python api/scripts/db_setup.py --db hamsa --fixups-only   # re-run demo_fixups.sql only (idempotent)

Order: v1 schema -> seed.sql -> whatsapp pivot -> retention_and_ingest -> batch_key -> demo_fixups.sql
Connection: DATABASE_URL (env or api/.env); --db replaces the database name in it.
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
SUPA = ROOT / "supabase"
STEPS = [
    ("v1 schema", SUPA / "migrations" / "20260918000000_hamsa_schema.sql"),
    ("seed", SUPA / "seed.sql"),
    ("whatsapp pivot", SUPA / "migrations" / "20260919000000_whatsapp_pivot.sql"),
    ("retention + ingest", SUPA / "migrations" / "20260919010000_retention_and_ingest.sql"),
    ("batch key", SUPA / "migrations" / "20260919020000_batch_key.sql"),
    ("demo fixups", SUPA / "demo_fixups.sql"),
]
COUNT_TABLES = ["products", "manufacturer_aliases", "batches", "nsq_alerts", "scan_events",
                "scan_evidence", "reports", "disputes", "consent_log", "audit_log",
                "manufacturer_domains", "wa_sessions", "wa_inbound_dedupe", "uploads", "nsq_ingest_runs"]


def base_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / "api" / ".env")
            url = os.getenv("DATABASE_URL")
        except ImportError:
            pass
    return url or "postgresql://postgres:postgres@localhost:5432/hamsa"


def conninfo_for(url: str, dbname: str | None) -> str:
    d = conninfo_to_dict(url)
    if dbname:
        d["dbname"] = dbname
    return make_conninfo(**d)


def run_file(c, label: str, path: Path) -> None:
    print(f"  applying {label}: {path.relative_to(ROOT)}")
    c.execute(path.read_text(encoding="utf-8"))


def create_db(url: str, name: str, reset: bool) -> None:
    with psycopg.connect(conninfo_for(url, "postgres"), autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists and reset:
            print(f"  dropping database {name}")
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            exists = None
        if not exists:
            print(f"  creating database {name}")
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def report(c) -> None:
    print("Row counts:")
    for t in COUNT_TABLES:
        n = c.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
        print(f"  {t:<22}{n}")
    rls = c.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity").fetchone()[0]
    print(f"  {'RLS-enabled tables':<22}{rls}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="target database name (default: the one in DATABASE_URL)")
    ap.add_argument("--reset", action="store_true", help="drop and recreate the database first")
    ap.add_argument("--fixups-only", action="store_true", help="only re-run demo_fixups.sql")
    a = ap.parse_args()

    url = base_url()
    name = a.db or conninfo_to_dict(url).get("dbname")
    if not name:
        sys.exit("No database name: pass --db or set DATABASE_URL")
    d = conninfo_to_dict(url)
    print(f"Target: {d.get('host', 'localhost')}:{d.get('port', 5432)}/{name}")

    try:
        if a.fixups_only:
            with psycopg.connect(conninfo_for(url, name), autocommit=True) as c:
                run_file(c, *STEPS[-1])
                report(c)
            return 0
        create_db(url, name, a.reset)
        with psycopg.connect(conninfo_for(url, name), autocommit=True) as c:
            if c.execute("SELECT to_regclass('public.products')").fetchone()[0]:
                sys.exit(f"Database {name} is already built. Use --reset to rebuild or --fixups-only.")
            for step in STEPS:
                run_file(c, *step)
            report(c)
    except psycopg.Error as e:
        sys.exit(f"ERROR: {type(e).__name__}: {e}")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
