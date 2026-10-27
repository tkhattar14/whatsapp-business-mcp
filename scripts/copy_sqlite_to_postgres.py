"""
One-time copy of the support bot's SQLite database into PostgreSQL.

Copies conversations, tickets, messages and relay state with their original IDs,
then moves PostgreSQL's ID counters past them so ticket numbers continue
(e.g. the next ticket after MMF-00002 is MMF-00003).

The PostgreSQL URL is read from the MIGRATE_TO_DATABASE_URL environment variable
(or the .env file) so it never appears on the command line or in shell history.

Usage:
    python scripts/copy_sqlite_to_postgres.py                    # copies data/support.db
    python scripts/copy_sqlite_to_postgres.py path/to/other.db
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from support_bot.store import TABLES, SupportStore, is_postgres_url  # noqa: E402

# Tables with auto-numbered IDs whose counters must continue after the copied rows
ID_TABLES = ("tickets", "messages", "relay_queue")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sqlite_path", nargs="?", default=os.path.join("data", "support.db"))
    args = parser.parse_args()

    load_dotenv(".env")
    url = os.getenv("MIGRATE_TO_DATABASE_URL", "")
    if not is_postgres_url(url):
        sys.exit("Set MIGRATE_TO_DATABASE_URL (in .env) to the PostgreSQL connection URL.")
    if not os.path.exists(args.sqlite_path):
        sys.exit(f"SQLite database not found: {args.sqlite_path}")

    source = sqlite3.connect(args.sqlite_path)
    source.row_factory = sqlite3.Row
    target = SupportStore(url)  # creates the tables if needed
    db = target.db

    existing = {t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("tickets", "messages")}
    if any(existing.values()):
        sys.exit(f"PostgreSQL already contains data ({existing}) - not copying, to avoid duplicates.")

    copied = {}
    with db.transaction():
        for table in TABLES:
            target_columns = {r["column_name"] for r in db.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}
            try:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                rows = []  # table didn't exist in this older database
            if not rows:
                copied[table] = 0
                continue
            columns = [c for c in rows[0].keys() if c in target_columns]
            placeholders = ", ".join("?" for _ in columns)
            db.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                [[row[c] for c in columns] for row in rows],
            )
            copied[table] = len(rows)

        for table in ID_TABLES:
            db.query(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}"
            )

    for table in TABLES:
        in_target = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        status = "OK" if in_target == copied[table] else "MISMATCH"
        print(f"{table:14} copied {copied[table]:5}   now in PostgreSQL {in_target:5}   {status}")

    last = db.one("SELECT ref FROM tickets ORDER BY id DESC LIMIT 1")
    print(f"Last ticket: {last['ref'] if last else 'none'} - new tickets will continue from the next number.")
    target.close()
    source.close()


if __name__ == "__main__":
    main()
