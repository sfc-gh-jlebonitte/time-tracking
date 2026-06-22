#!/usr/bin/env python3
"""
Generate meeting summaries from local transcript recorder files via Snowflake Cortex.

Scans a recordings directory for `meeting_transcript.txt` files, matches them
to CSV rows by date/time, then generates a 2-3 sentence summary using Cortex Complete.

Usage (via run.sh):
    bash run.sh summarize [--dry-run]

Config (from .secrets/snowhouse.env):
    SNOWFLAKE_RECORDINGS_DIR  — path to transcriptrecorder recordings folder
    SNOWHOUSE_SNOWFLAKE_*     — standard connection vars
"""

import csv
import glob
import os
import re
import sys
import time
from pathlib import Path

import snowflake.connector

MATCH_WINDOW_MINUTES = 90
TRANSCRIPT_CHAR_LIMIT = 8000


# ---------------------------------------------------------------------------
# Snowflake connection
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
# Transcript index
# ---------------------------------------------------------------------------

def build_transcript_index(recordings_base: str) -> dict[str, list[tuple[int, str]]]:
    """Index: {date_str: [(minutes_from_midnight, path), ...]}"""
    index: dict[str, list] = {}
    pattern = os.path.join(recordings_base, "**", "meeting_transcript.txt")
    for path in glob.glob(pattern, recursive=True):
        folder = Path(path).parent.name
        m = re.match(r"recording_(\d{4}-\d{2}-\d{2})_(\d{4})_", folder)
        if not m:
            continue
        date_str = m.group(1)
        hhmm = m.group(2)
        mins = int(hhmm[:2]) * 60 + int(hhmm[2:])
        index.setdefault(date_str, []).append((mins, path))
    return index


def find_transcript(date_str: str, time_str: str, index: dict) -> str | None:
    entries = index.get(date_str, [])
    if not entries:
        return None
    parts = time_str.split(":")
    meeting_mins = int(parts[0]) * 60 + int(parts[1])
    best_path, best_diff = None, MATCH_WINDOW_MINUTES + 1
    for rec_mins, path in entries:
        diff = abs(rec_mins - meeting_mins)
        if diff < best_diff:
            best_diff, best_path = diff, path
    return best_path if best_diff <= MATCH_WINDOW_MINUTES else None


def read_transcript(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(TRANSCRIPT_CHAR_LIMIT * 2)[:TRANSCRIPT_CHAR_LIMIT]
    except Exception as e:
        print(f"  [WARN] Could not read {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def generate_summary(conn, transcript: str, title: str, customer: str) -> str | None:
    prompt = (
        "You are summarizing a business meeting transcript for a Snowflake specialist's activity log.\n\n"
        f"Meeting title: {title}\nCustomer: {customer}\n\n"
        f"TRANSCRIPT (may be truncated):\n{transcript}\n\n"
        "Write a 2-3 sentence summary. Focus on: who attended (first names + company), "
        "what was discussed, and key outcomes or next steps.\n"
        "Style: \"[Names] from Snowflake, along with [customer names] from [company], discussed [topics]. [Outcome].\"\n"
        "Return ONLY the summary text."
    )
    safe = prompt.replace("'", "\\'")
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-large2', '{safe}') AS summary")
        row = cur.fetchone()
        return row[0].strip() if row and row[0] else None
    except Exception as e:
        print(f"  [ERROR] Cortex Complete failed: {e}")
        return None


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def process_csv(csv_path: str, index: dict, conn, dry_run: bool) -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    filled = no_transcript = 0
    for i, row in enumerate(rows):
        if row.get("Summary", "").strip():
            continue

        date_str = row.get("Date", "").strip()
        time_str = row.get("Time", "").strip()
        if not date_str or not time_str:
            continue

        path = find_transcript(date_str, time_str, index)
        if not path:
            no_transcript += 1
            continue

        print(f"  [{date_str} {time_str}] {row.get('Customer','')[:30]} — {Path(path).parent.name}")
        transcript = read_transcript(path)
        if not transcript:
            continue

        if not dry_run:
            summary = generate_summary(conn, transcript, row.get("Meeting Title",""), row.get("Customer",""))
            if summary:
                rows[i]["Summary"] = summary
                print(f"    → {summary[:80]}...")
                filled += 1
            else:
                print(f"    → [WARN] No summary generated")
            time.sleep(0.3)
        else:
            print(f"    → [dry-run] would generate summary")
            filled += 1

    print(f"  Filled: {filled} | No transcript found: {no_transcript}")

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
    ap = argparse.ArgumentParser(description="Generate meeting summaries from local transcripts")
    ap.add_argument("--dry-run", action="store_true", help="Show matches without writing to CSVs")
    args = ap.parse_args(argv)

    script_dir = Path(__file__).resolve().parent.parent
    _load_env(script_dir / ".secrets")

    recordings_dir = os.environ.get("SNOWFLAKE_RECORDINGS_DIR", "")
    csv_dir = script_dir / "output"

    if not recordings_dir:
        print("ERROR: SNOWFLAKE_RECORDINGS_DIR not set in .secrets/snowhouse.env", file=sys.stderr)
        print("  Set it to the path of your transcriptrecorder recordings folder.", file=sys.stderr)
        return 1

    if not os.path.isdir(recordings_dir):
        print(f"ERROR: SNOWFLAKE_RECORDINGS_DIR does not exist: {recordings_dir}", file=sys.stderr)
        return 1

    print("Building transcript index...")
    index = build_transcript_index(recordings_dir)
    total = sum(len(v) for v in index.values())
    print(f"  Found {total} transcripts across {len(index)} dates")

    print("Connecting to Snowflake...")
    conn = _connect() if not args.dry_run else None

    csv_files = sorted(csv_dir.glob("week_*.csv"))
    print(f"Found {len(csv_files)} CSV files\n")

    grand_total = 0
    for path in csv_files:
        print(f"Processing: {path.name}")
        n = process_csv(str(path), index, conn, args.dry_run)
        grand_total += n

    if conn:
        conn.close()

    print(f"\nDone. Total summaries {'found' if args.dry_run else 'added'}: {grand_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
