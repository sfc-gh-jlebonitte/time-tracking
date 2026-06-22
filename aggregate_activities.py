"""
aggregate_activities.py — Load weekly SE activity CSVs from Google Drive into Snowflake.

Usage:
    bash run.sh aggregate          # load all CSVs from the shared Drive folder
    bash run.sh aggregate --dry-run # preview without writing to Snowflake
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.envfile import apply_env, load_env_file

HERE = Path(__file__).resolve().parent

DRIVE_FOLDER_ID = "12Vf2C7TDf2iTSF5c2-5NF0lvnZaD3cjZ"
TARGET_TABLE   = "TEMP.JLEBONITTE_EDA_ACTIVITY_TRACKING.SE_WEEKLY_ACTIVITIES"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_COL_MAP = {
    "Date":                  "MEETING_DATE",
    "Day":                   "MEETING_DAY",
    "Time":                  "MEETING_TIME",
    "Meeting Title":         "MEETING_TITLE",
    "Customer":              "CUSTOMER",
    "Source":                "SOURCE",
    "SF Account ID":         "SF_ACCOUNT_ID",
    "SF Activity ID":        "SF_ACTIVITY_ID",
    "Use Case Tagged in SF": "USE_CASE_TAGGED_IN_SF",
    "Opportunity Name":      "OPPORTUNITY_NAME",
    "Opportunity ID":        "OPPORTUNITY_ID",
    "Use Case Name":         "USE_CASE_NAME",
    "Use Case ID":           "USE_CASE_ID",
    "AE Name":               "AE_NAME",
    "AE Email":              "AE_EMAIL",
    "Use Case Lead SE":      "USE_CASE_LEAD_SE",
    "Meeting SE Name":       "MEETING_SE_NAME",
    "Meeting SE Email":      "MEETING_SE_EMAIL",
    "Summary":               "SUMMARY",
    "Next Steps":            "NEXT_STEPS",
    "Attendees":             "ATTENDEES",
    "Call URL":              "CALL_URL",
    "GCal Link":             "GCAL_LINK",
}


def _drive_service(secrets_dir: Path):
    import google.auth
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = secrets_dir / "google_token.json"
    creds_path = secrets_dir / "google_credentials.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds and not {"https://www.googleapis.com/auth/drive.readonly"}.issubset(creds.scopes or []):
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            adc_creds, _ = google.auth.default(scopes=SCOPES)
            creds = adc_creds  # type: ignore[assignment]
        token_path.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_csvs(service, folder_id: str) -> list[dict]:
    """List all CSVs recursively across team member subfolders."""
    files = []

    # Get top-level items
    result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=200,
    ).execute()

    for item in result.get("files", []):
        if item["mimeType"] == "application/vnd.google-apps.folder":
            # Recurse into team member subfolder, tag with SE name
            sub = service.files().list(
                q=f"'{item['id']}' in parents and mimeType='text/csv' and trashed=false",
                fields="files(id, name)",
                pageSize=200,
            ).execute()
            for f in sub.get("files", []):
                f["se_name"] = item["name"]
                files.append(f)
        elif item["mimeType"] == "text/csv":
            item["se_name"] = None
            files.append(item)

    return files


def _download_csv(service, file_id: str) -> list[dict]:
    content = service.files().get_media(fileId=file_id).execute()
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


_SF_ID_RE = re.compile(r'^[a-zA-Z0-9]{15,18}$')

def _clean_sf_id(val: str | None) -> str | None:
    if not val:
        return None
    v = val.strip()
    return v if _SF_ID_RE.match(v) else None


_FOLDER_TO_FULL_NAME = {
    "jlebonitte":  "Jim Lebonitte",
    "palapaty":    "Phani Alapaty",
    "psheehan":    "Patrick Sheehan",
    "smitchener":  "Steve Mitchener",
    "mhamilton":   "Michael Hamilton",
}


def _parse_rows(raw_rows: list[dict], source_file: str, se_name: str | None = None) -> list[dict]:
    out = []
    loaded_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in raw_rows:
        row: dict[str, Any] = {sf: None for sf in _COL_MAP.values()}
        row["SOURCE_FILE"] = source_file
        row["LOADED_AT"] = loaded_at
        for csv_col, sf_col in _COL_MAP.items():
            val = r.get(csv_col, "").strip()
            if val:
                # Sanitize SF ID columns to avoid GCal/calendar IDs leaking in
                if sf_col in ("SF_ACCOUNT_ID", "SF_ACTIVITY_ID", "OPPORTUNITY_ID", "USE_CASE_ID"):
                    val = _clean_sf_id(val)
                elif sf_col == "USE_CASE_TAGGED_IN_SF":
                    val = val if val.lower() in ("yes", "no", "unknown") else None
                row[sf_col] = val or None
        # Normalize SE name: map folder names to full canonical names
        canonical_se = _FOLDER_TO_FULL_NAME.get(se_name or "", se_name) if se_name else None
        if not row["MEETING_SE_NAME"] and canonical_se:
            row["MEETING_SE_NAME"] = canonical_se
        elif row["MEETING_SE_NAME"] and row["MEETING_SE_NAME"].lower() in _FOLDER_TO_FULL_NAME:
            row["MEETING_SE_NAME"] = _FOLDER_TO_FULL_NAME[row["MEETING_SE_NAME"].lower()]
        if not row["MEETING_DATE"]:
            continue
        out.append(row)
    return out


def _connect_snowflake():
    import snowflake.connector
    return snowflake.connector.connect(
        account=os.environ["SNOWHOUSE_SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWHOUSE_SNOWFLAKE_USER"],
        authenticator=os.environ.get("SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
        warehouse=os.environ.get("SNOWHOUSE_SNOWFLAKE_WAREHOUSE", "SE_WH"),
        role=os.environ.get("SNOWHOUSE_SNOWFLAKE_ROLE") or None,
    )


def _validate_sf_ids(con, all_rows: list[dict]) -> list[dict]:
    """
    Cross-check SF IDs against reference tables. Nulls out any IDs that don't
    exist in Salesforce, logging warnings so data stays clean on load.

    Reference tables:
      SF_ACCOUNT_ID  → SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS
      OPPORTUNITY_ID → MDM.MDM_INTERFACES.DIM_USE_CASE (OPP_ID)
      USE_CASE_ID    → MDM.MDM_INTERFACES.DIM_USE_CASE
      SF_ACTIVITY_ID → SALES.SE_REPORTING.DIM_SE_ACTIVITY
    """
    checks = {
        "SF_ACCOUNT_ID":  ("SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS", "SALESFORCE_ACCOUNT_ID"),
        "OPPORTUNITY_ID": ("MDM.MDM_INTERFACES.DIM_USE_CASE",             "OPP_ID"),
        "USE_CASE_ID":    ("MDM.MDM_INTERFACES.DIM_USE_CASE",             "USE_CASE_ID"),
        "SF_ACTIVITY_ID": ("SALES.SE_REPORTING.DIM_SE_ACTIVITY",          "ACTIVITY_ID"),
    }

    for col, (table, ref_col) in checks.items():
        ids = {r[col] for r in all_rows if r.get(col)}
        if not ids:
            continue
        ph = ",".join(["%s"] * len(ids))
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT {ref_col} FROM {table} WHERE {ref_col} IN ({ph})",
                    list(ids),
                )
                valid = {str(r[0]).strip() for r in cur.fetchall() if r[0]}
        except Exception as e:
            print(f"  [warn] Could not validate {col} against {table}: {e}")
            continue

        invalid = ids - valid
        if invalid:
            print(f"  [warn] {col}: {len(invalid)} IDs not found in {table} — nulling out: {sorted(invalid)[:5]}{'...' if len(invalid) > 5 else ''}")
            for r in all_rows:
                if r.get(col) in invalid:
                    r[col] = None
        else:
            print(f"  [ok]   {col}: all {len(ids)} IDs verified in {table}")

    # Cross-check: USE_CASE_ID must belong to the same account as SF_ACCOUNT_ID.
    # A use case from a different account leaking onto a meeting is a false association.
    uc_account_pairs = [
        (r["USE_CASE_ID"], r["SF_ACCOUNT_ID"])
        for r in all_rows
        if r.get("USE_CASE_ID") and r.get("SF_ACCOUNT_ID")
    ]
    if uc_account_pairs:
        uc_ids = list({p[0] for p in uc_account_pairs})
        ph = ",".join(["%s"] * len(uc_ids))
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"SELECT USE_CASE_ID, ACCOUNT_ID FROM MDM.MDM_INTERFACES.DIM_USE_CASE WHERE USE_CASE_ID IN ({ph})",
                    uc_ids,
                )
                uc_to_account = {str(r[0]).strip(): str(r[1]).strip() for r in cur.fetchall()}
        except Exception as e:
            print(f"  [warn] Could not validate USE_CASE_ID/SF_ACCOUNT_ID cross-check: {e}")
            uc_to_account = {}

        mismatched = 0
        for r in all_rows:
            uc = r.get("USE_CASE_ID")
            acct = r.get("SF_ACCOUNT_ID")
            if uc and acct and uc_to_account.get(uc) and uc_to_account[uc] != acct:
                r["USE_CASE_ID"] = None
                r["USE_CASE_NAME"] = None
                mismatched += 1
        if mismatched:
            print(f"  [warn] USE_CASE_ID/SF_ACCOUNT_ID mismatch: nulled {mismatched} cross-account use case associations")
        else:
            print(f"  [ok]   USE_CASE_ID account cross-check passed")

    return all_rows



    import snowflake.connector
    return snowflake.connector.connect(
        account=os.environ["SNOWHOUSE_SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWHOUSE_SNOWFLAKE_USER"],
        authenticator=os.environ.get("SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
        warehouse=os.environ.get("SNOWHOUSE_SNOWFLAKE_WAREHOUSE", "SE_WH"),
        role=os.environ.get("SNOWHOUSE_SNOWFLAKE_ROLE") or None,
    )


def main(dry_run: bool = False) -> int:
    apply_env(load_env_file(HERE / ".secrets" / "snowhouse.env"), into=os.environ)

    secrets_dir = HERE / ".secrets"
    print(f"Listing CSVs in Drive folder {DRIVE_FOLDER_ID}...")
    service = _drive_service(secrets_dir)
    files = _list_csvs(service, DRIVE_FOLDER_ID)
    if not files:
        print("  No CSV files found in folder.")
        return 0
    print(f"  Found {len(files)} file(s): {[f['name'] for f in files]}")

    all_rows: list[dict] = []
    for f in files:
        print(f"  Downloading {f['name']}...")
        raw = _download_csv(service, f["id"])
        rows = _parse_rows(raw, f["name"], se_name=f.get("se_name"))
        print(f"    {len(rows)} rows parsed (SE: {f.get('se_name', 'unknown')})")
        all_rows.extend(rows)

    print(f"\nTotal rows to load: {len(all_rows)}")
    if not all_rows:
        print("Nothing to load.")
        return 0

    if dry_run:
        print("[dry-run] Skipping Snowflake write.")
        for r in all_rows[:3]:
            print(" ", {k: v for k, v in r.items() if v and k not in ("SUMMARY", "NEXT_STEPS")})
        return 0

    print(f"Connecting to Snowflake...")
    con = _connect_snowflake()
    cols = list(_COL_MAP.values()) + ["SOURCE_FILE", "LOADED_AT"]
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)

    try:
        print("Validating SF referential integrity...")
        all_rows = _validate_sf_ids(con, all_rows)

        with con.cursor() as cur:
            print(f"Truncating {TARGET_TABLE}...")
            cur.execute(f"TRUNCATE TABLE {TARGET_TABLE}")

            print(f"Inserting {len(all_rows)} rows...")
            batch = [[r[c] for c in cols] for r in all_rows]
            cur.executemany(
                f"INSERT INTO {TARGET_TABLE} ({col_list}) VALUES ({placeholders})",
                batch,
            )
            print(f"Done. {len(all_rows)} rows loaded into {TARGET_TABLE}")

            # Normalize CUSTOMER to canonical SF account name from Salesforce.
            # This overwrites whatever string the AI or CSV had with the
            # authoritative account name, for any row where we resolved an account ID.
            print("Normalizing customer names from Salesforce account records...")
            cur.execute(f"""
                MERGE INTO {TARGET_TABLE} a
                USING (
                    SELECT SALESFORCE_ACCOUNT_ID, MIN(SALESFORCE_ACCOUNT_NAME) AS ACCOUNT_NAME
                    FROM SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS
                    GROUP BY 1
                ) d ON a.SF_ACCOUNT_ID = d.SALESFORCE_ACCOUNT_ID
                WHEN MATCHED THEN UPDATE SET a.CUSTOMER = d.ACCOUNT_NAME
            """)
            print(f"  Normalized {cur.rowcount} rows")

            # Set USE_CASE_TAGGED_IN_SF = 'Yes' for any meeting where the account
            # has at least one ACTIVE use case — not tech won, not won, not deployed,
            # not lost. Stage 1-5 only (stage 6 = Implementation Complete = done).
            print("Updating use case tagged status from Salesforce...")
            cur.execute(f"""
                MERGE INTO {TARGET_TABLE} a
                USING (
                    SELECT DISTINCT d.ACCOUNT_ID
                    FROM MDM.MDM_INTERFACES.DIM_USE_CASE d
                    WHERE d.IS_LOST      = FALSE
                      AND d.IS_TECH_WON  = FALSE
                      AND d.IS_WON       = FALSE
                      AND d.IS_DEPLOYED  = FALSE
                      AND d.STAGE_NUMBER BETWEEN 1 AND 5
                ) uc ON a.SF_ACCOUNT_ID = uc.ACCOUNT_ID
                WHEN MATCHED AND (a.USE_CASE_TAGGED_IN_SF IS NULL OR a.USE_CASE_TAGGED_IN_SF != 'Yes') THEN
                    UPDATE SET a.USE_CASE_TAGGED_IN_SF = 'Yes'
            """)
            print(f"  Updated {cur.rowcount} rows to use case tagged = Yes")
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(main(dry_run=args.dry_run))
