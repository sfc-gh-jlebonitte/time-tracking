#!/usr/bin/env python3
"""
Back-fill empty CSV Summary fields using Gong takeaways from Snowflake.

Matches CSV meeting rows to Gong records using date, time proximity, title
similarity, and account name. Writes the Gong 'recap' into the Summary column.

Usage (via run.sh):
    bash run.sh backfill [--weeks N] [--dry-run]

Config (from .secrets/snowhouse.env):
    SNOWFLAKE_SE_NAME       — full name to filter Gong participants (e.g. "Jim Lebonitte")
    SNOWHOUSE_SNOWFLAKE_*   — standard connection vars
"""

import csv
import glob
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import snowflake.connector

MATCH_WINDOW_MINUTES = 90
MIN_MATCH_SCORE = 20


# ---------------------------------------------------------------------------
# Snowflake connection (shared env pattern)
# ---------------------------------------------------------------------------

def _connect() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        account=os.environ["SNOWHOUSE_SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWHOUSE_SNOWFLAKE_USER"],
        authenticator=os.environ.get("SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
        warehouse=os.environ.get("SNOWHOUSE_SNOWFLAKE_WAREHOUSE", "SE_WH"),
        role=os.environ.get("SNOWHOUSE_SNOWFLAKE_ROLE") or None,
    )


def _load_env(secrets_dir: Path) -> None:
    env_path = secrets_dir / "snowhouse.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Gong data fetch
# ---------------------------------------------------------------------------

def fetch_gong_meetings(conn, se_name: str, weeks_back: int) -> list[dict]:
    since = (date.today() - timedelta(weeks=weeks_back)).isoformat()
    sql = f"""
    SELECT
        ACTIVITY_DATE::VARCHAR                          AS date,
        TO_CHAR(ACTIVITY_TIMESTAMP, 'HH24:MI')         AS time,
        TRIM(REPLACE(SUBJECT, '[Sent] ', ''))           AS subject,
        CRM_ACCOUNT_NAME                                AS account,
        PARTICIPANT_NAMES,
        TAKEAWAYS::VARCHAR                              AS takeaways
    FROM SALES.RAVEN.ALL_ENGAGEMENTS_PREPED_VIEW
    WHERE ACTIVITY_DATE >= '{since}'
      AND REGEXP_LIKE(PARTICIPANT_NAMES, '.*{re.escape(se_name)}.*', 'i')
      AND TYPE = 'MEETING'
      AND TAKEAWAYS IS NOT NULL AND TAKEAWAYS::VARCHAR != 'null'
    ORDER BY ACTIVITY_DATE, ACTIVITY_TIMESTAMP
    """
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0].lower() for d in cur.description]
    meetings = []
    for row in cur.fetchall():
        m = dict(zip(cols, row))
        try:
            tw = json.loads(m["takeaways"])
            m["recap"] = tw.get("recap", "").strip().rstrip("\n")
        except Exception:
            m["recap"] = ""
        h, mi = map(int, m["time"].split(":"))
        m["time_mins"] = h * 60 + mi
        meetings.append(m)
    print(f"  Loaded {len(meetings)} Gong meetings with takeaways (since {since})")
    return meetings


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    wa = set(_normalize(a).split())
    wb = set(_normalize(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _account_match(csv_customer: str, gong_account: str) -> bool:
    if not csv_customer or not gong_account:
        return False
    c, g = _normalize(csv_customer), _normalize(gong_account)
    for w in [w for w in c.split() if len(w) > 3]:
        if w in g:
            return True
    for w in [w for w in g.split() if len(w) > 3]:
        if w in c:
            return True
    return False


def find_best_match(row: dict, gong_meetings: list[dict]) -> tuple[dict | None, float]:
    date_str = row.get("Date", "").strip()
    time_str = row.get("Time", "").strip()
    title    = row.get("Meeting Title", "").strip()
    customer = row.get("Customer", "").strip()

    try:
        h, mi = map(int, time_str.split(":"))
        csv_mins = h * 60 + mi
    except Exception:
        csv_mins = None

    same_day = [m for m in gong_meetings if m["date"] == date_str]
    if not same_day:
        return None, 0.0

    best, best_score = None, 0.0
    for m in same_day:
        score = _title_similarity(title, m["subject"]) * 50
        if _account_match(customer, m["account"]):
            score += 30
        if csv_mins is not None:
            diff = abs(m["time_mins"] - csv_mins)
            if diff <= MATCH_WINDOW_MINUTES:
                score += max(0, 20 - (diff / MATCH_WINDOW_MINUTES) * 20)
        if score > best_score:
            best_score, best = score, m

    return (best, best_score) if best_score >= MIN_MATCH_SCORE else (None, 0.0)


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def process_csv(csv_path: str, gong_meetings: list[dict], dry_run: bool) -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    filled = 0
    for i, row in enumerate(rows):
        if row.get("Summary", "").strip():
            continue
        match, score = find_best_match(row, gong_meetings)
        if not match or not match.get("recap"):
            continue

        print(f"  [{row['Date']} {row['Time']}] {row.get('Customer','')[:30]}")
        print(f"    → Gong: '{match['subject'][:50]}' (score={score:.0f})")
        print(f"    → Recap: {match['recap'][:90]}...")
        if not dry_run:
            rows[i]["Summary"] = match["recap"]
        filled += 1

    if filled > 0 and not dry_run:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return filled


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Back-fill CSV summaries from Gong takeaways")
    ap.add_argument("--weeks", type=int, default=26, help="How many weeks back to fetch Gong data (default: 26)")
    ap.add_argument("--dry-run", action="store_true", help="Show matches without writing to CSVs")
    args = ap.parse_args(argv)

    script_dir = Path(__file__).resolve().parent.parent
    _load_env(script_dir / ".secrets")

    se_name  = os.environ.get("SNOWFLAKE_SE_NAME", "")
    csv_dir  = script_dir / "output"

    if not se_name:
        print("ERROR: SNOWFLAKE_SE_NAME not set in .secrets/snowhouse.env", file=sys.stderr)
        return 1

    print(f"Connecting to Snowflake...")
    conn = _connect()
    print(f"Fetching Gong meetings for '{se_name}' (last {args.weeks} weeks)...")
    gong_meetings = fetch_gong_meetings(conn, se_name, args.weeks)
    conn.close()

    csv_files = sorted(csv_dir.glob("week_*.csv"))
    print(f"Found {len(csv_files)} CSV files\n")

    total = 0
    for path in csv_files:
        print(f"Processing: {path.name}")
        n = process_csv(str(path), gong_meetings, args.dry_run)
        if n:
            status = "[dry-run]" if args.dry_run else "saved"
            print(f"  {n} summaries {status}")
        else:
            print(f"  No new matches")
        total += n

    print(f"\nDone. Total Gong summaries {'found' if args.dry_run else 'added'}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
