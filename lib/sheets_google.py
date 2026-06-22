from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

_SF_BASE = "https://snowflake.lightning.force.com/lightning/r"


def _sf_url(obj_type: str, obj_id: str) -> str:
    return f"{_SF_BASE}/{obj_type}/{obj_id}/view" if obj_id else ""


_HEADERS = [
    "Date",
    "Day",
    "Time",
    "Meeting Title",
    "Customer",
    "Source",
    "SF Account ID",
    "SF Activity ID",
    "Use Case Tagged in SF",
    "Opportunity Name",
    "Opportunity ID",
    "Use Case Name",
    "Use Case ID",
    "AE Name",
    "AE Email",
    "Use Case Lead SE",
    "Meeting SE Name",
    "Meeting SE Email",
    "Summary",
    "Next Steps",
    "Attendees",
    "Call URL",
    "GCal Link",
]


def write_weekly_csv(
    out_path: Path,
    week_start: date,
    week_end: date,
    rows: list[dict[str, Any]],
) -> Path:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_HEADERS)
        for r in rows:
            dt: datetime = r["dt"]
            uc_id = r.get("uc_id") or ""
            has_uc = bool(r.get("has_active_uc"))
            writer.writerow([
                dt.strftime("%Y-%m-%d"),
                dt.strftime("%A"),
                dt.strftime("%H:%M"),
                r.get("title", ""),
                r.get("customer", ""),
                r.get("source", ""),
                r.get("sf_account_id", ""),
                r.get("sf_activity_id", ""),
                "Yes" if r.get("uc_id") or r.get("sf_use_case_id") else ("No" if r.get("sf_account_id") else "Unknown"),
                r.get("opp", ""),
                r.get("sf_opp_id", ""),
                r.get("uc_name", "") or "; ".join(r.get("use_cases") or []),
                uc_id or (r.get("sf_use_case_id") or ""),
                "" if has_uc else (r.get("ae_name") or ""),
                "" if has_uc else (r.get("ae_email") or ""),
                r.get("uc_lead_se_name", ""),
                r.get("se_name", ""),
                r.get("se_email", ""),
                (r.get("summary") or "")[:2000],
                (r.get("next_steps") or "")[:2000],
                ", ".join(r.get("attendees") or []),
                r.get("call_url", ""),
                r.get("gcal_html_link", ""),
            ])
    return out_path
