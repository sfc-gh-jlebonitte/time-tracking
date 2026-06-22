"""
weekly_report.py — Weekly customer activity report from Google Calendar + Snowhouse.

Scrapes last week's customer meetings (Mon–Sun), enriches each with Gong/Zoom
transcript summaries and Salesforce use cases, and writes a per-customer Markdown
file to output/.

Usage:
    python weekly_report.py                    # last full Mon–Sun week
    python weekly_report.py --week 2025-01-06  # week starting on that Monday

Auth:
    GCal:       .secrets/google_credentials.json  (OAuth Desktop App)
    Snowflake:  .secrets/snowhouse.env
"""
from __future__ import annotations

import argparse
import logging
import os
import re as _re
import time as _time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from lib.calendar_google import (
    GoogleCalendarAuth,
    event_to_meeting,
    iter_events,
    load_google_calendar_service,
)
from lib.customer_attribution import CustomerAttributor, canonicalize_customer
from lib.customer_meetings import CustomerMeetingHeuristics, is_customer_meeting
from lib.domain_resolver import DomainResolver
from lib.envfile import apply_env, load_env_file
from lib.internal_calls import is_internal_call
from lib.salesforce_accounts import fetch_all_se_accounts
from lib.sheets_google import write_weekly_csv

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weekly")
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Snowflake connection
# ---------------------------------------------------------------------------

def _opt_env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _req_env(name: str) -> str:
    v = _opt_env(name)
    if not v:
        raise SystemExit(f"Missing required env var {name!r}. Check .secrets/snowhouse.env")
    return v


def _normalize_account(v: str) -> str:
    v = v.strip().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    for suffix in (".snowflakecomputing.com", ".privatelink.snowflakecomputing.com"):
        if v.lower().endswith(suffix):
            v = v[: -len(suffix)]
    return v


def _connect_snowflake():
    try:
        import snowflake.connector  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "Missing snowflake-connector-python. Run:\n"
            "  .venv/bin/pip install snowflake-connector-python"
        ) from e

    password = _opt_env("SNOWHOUSE_SNOWFLAKE_PASSWORD")
    authenticator = _opt_env("SNOWHOUSE_SNOWFLAKE_AUTHENTICATOR")
    if password is None and authenticator is None:
        authenticator = "externalbrowser"

    kwargs: dict[str, Any] = dict(
        account=_normalize_account(_req_env("SNOWHOUSE_SNOWFLAKE_ACCOUNT")),
        user=_req_env("SNOWHOUSE_SNOWFLAKE_USER"),
        warehouse=_req_env("SNOWHOUSE_SNOWFLAKE_WAREHOUSE"),
        database=_req_env("SNOWHOUSE_SNOWFLAKE_DATABASE"),
        schema=_req_env("SNOWHOUSE_SNOWFLAKE_SCHEMA"),
        role=_opt_env("SNOWHOUSE_SNOWFLAKE_ROLE"),
    )
    if password is not None:
        kwargs["password"] = password
    if authenticator is not None:
        kwargs["authenticator"] = authenticator
    return snowflake.connector.connect(**kwargs)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MeetingItem:
    dt: datetime
    customer: str
    title: str
    customer_attribution: str | None
    sources: set[str]
    is_internal: bool = False
    opp: str | None = None
    opp_value: float | None = None
    sf_account_id: str | None = None
    sf_activity_id: str | None = None
    sf_opp_id: str | None = None
    sf_use_case_id: str | None = None
    se_name: str | None = None
    se_email: str | None = None
    use_cases: list[str] | None = None
    summary: str | None = None
    next_steps: str | None = None
    key_points: str | None = None
    call_url: str | None = None
    transcript_url: str | None = None
    recording_password: str | None = None
    notes_source: str | None = None
    primary_source: str | None = None
    gcal_html_link: str | None = None
    gcal_conference_url: str | None = None
    gcal_attendees: list[str] | None = None
    gcal_description: str | None = None


# ---------------------------------------------------------------------------
# Snowhouse helpers (adapted from report_activity_all_sources_md.py)
# ---------------------------------------------------------------------------

def _maybe_str(row: dict[str, Any], key: str) -> str | None:
    v = row.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _uniq(xs: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in xs:
        k = (x or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


def _fmt_usd(v: float | None) -> str | None:
    if v is None:
        return None
    try:
        n = float(v)
    except Exception:
        return None
    if abs(n) >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:,.0f}"


def _md_escape(s: str) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _pick_title(row: dict[str, Any]) -> str:
    for k in ("MEETING_TITLE", "GONG_TITLE", "ACTIVITY_DESCRIPTION"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(untitled meeting)"


def _pick_customer(row: dict[str, Any]) -> str:
    for k in ("CUSTOMER_ACCOUNT", "ACCOUNT_NAME"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(unknown customer)"


def _join_context(row: dict[str, Any]) -> str:
    return "\n".join(
        s for s in [_maybe_str(row, k) for k in ("SUMMARY", "NEXT_STEPS", "KEY_POINTS")] if s
    ).strip()


def _score_row(row: dict[str, Any]) -> int:
    score = 0
    if _maybe_str(row, "CUSTOMER_ACCOUNT") or _maybe_str(row, "ACCOUNT_NAME"):
        score += 50
    if _maybe_str(row, "OPP_NAME"):
        score += 15
    if _maybe_str(row, "CALL_URL"):
        score += 12
    if _maybe_str(row, "TRANSCRIPT_URL"):
        score += 8
    score += min(30, len(_maybe_str(row, "SUMMARY") or "") // 200 * 10)
    score += min(20, len(_maybe_str(row, "NEXT_STEPS") or "") // 200 * 10)
    return score


def _dedupe(rows: list[tuple[datetime, dict[str, Any]]], tzinfo) -> list[tuple[datetime, dict[str, Any]]]:
    dedup: dict[tuple, tuple[int, datetime, dict[str, Any]]] = {}
    for dt, row in rows:
        mid = str(row.get("MEETING_ID") or "").strip()
        if mid and mid.lower() != "none":
            key: tuple = ("zoom", mid)
        else:
            dt_s = (dt if dt.tzinfo else dt.replace(tzinfo=tzinfo)).isoformat()
            key = ("t", dt_s, _pick_title(row).lower(), _maybe_str(row, "CALL_URL") or "")
        score = _score_row(row)
        prev = dedup.get(key)
        if prev is None or score > prev[0]:
            dedup[key] = (score, dt, row)
    return sorted([(dt, r) for _, dt, r in dedup.values()], key=lambda x: x[0])


_UNTRUSTED = {"pre"}


def _resolve_customer(
    row: dict[str, Any],
    title: str,
    attributor: CustomerAttributor,
    preferred: set[str],
) -> tuple[str, str | None]:
    raw = _pick_customer(row)
    ctx = _join_context(row)

    if raw != "(unknown customer)":
        canon = canonicalize_customer(raw, preferred_accounts=preferred)
        if not canon or canon.strip().lower() in _UNTRUSTED:
            raw = "(unknown customer)"
        else:
            if canon not in preferred and title:
                res = attributor.attribute(title=title, context=ctx)
                if res.customer and res.confidence >= 0.85:
                    inferred = canonicalize_customer(res.customer, preferred_accounts=preferred)
                    if inferred and inferred != canon:
                        return inferred, "Inferred (override)"
            return canon, None

    if title:
        res = attributor.attribute(title=title, context=ctx)
        if res.customer and res.confidence >= 0.75:
            badge = "Inferred (high)" if res.confidence >= 0.85 else "Inferred (medium)"
            return canonicalize_customer(res.customer, preferred_accounts=preferred), badge

    return "(unknown customer)", None


def _tokenize(s: str) -> set[str]:
    s = (s or "").lower()
    for ch in "&|/:-—–,()[]{}":
        s = s.replace(ch, " ")
    stop = {"snowflake", "internal", "prep", "pre", "meeting", "call", "sync", "session"}
    return {t for t in s.split() if t and t not in stop and len(t) > 1}


def _match_gcal(g_title: str, g_dt: datetime, g_conf: str | None, items: list[MeetingItem]) -> int | None:
    best_idx, best = None, 0.0
    gt = _tokenize(g_title)
    for i, s in enumerate(items):
        if abs((s.dt - g_dt).total_seconds()) > 20 * 60:
            continue
        st = _tokenize(s.title)
        j = len(gt & st) / max(1, len(gt | st)) if gt and st else 0.0
        score = j
        if g_conf and s.call_url and (g_conf in s.call_url or s.call_url in g_conf):
            score += 1.5
        if score > best:
            best, best_idx = score, i
    return best_idx if best_idx is not None and best >= 0.25 else None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _render(
    items: list[MeetingItem],
    week_start: date,
    week_end: date,
) -> str:
    items.sort(key=lambda x: x.dt)

    customers = sorted({
        it.customer for it in items
        if it.customer and it.customer != "(unknown customer)" and not it.is_internal
    })

    lines: list[str] = []
    lines.append(f"# Weekly activity — {week_start} → {week_end}")
    lines.append("")
    lines.append(f"- **Generated**: {datetime.now().astimezone().isoformat()}")
    lines.append(f"- **Window**: {week_start} → {week_end}")
    lines.append(f"- **Meetings**: {len(items)}")
    lines.append(f"- **Customers**: {len(customers)}")
    lines.append("")

    if customers:
        lines.append("## Customers met with")
        lines.append("")
        for c in customers:
            lines.append(f"- {c}")
        lines.append("")

    # Group by customer (customer meetings), then chronological
    by_customer: dict[str, list[MeetingItem]] = defaultdict(list)
    unattributed: list[MeetingItem] = []
    for it in items:
        if it.is_internal:
            continue
        if it.customer == "(unknown customer)":
            unattributed.append(it)
        else:
            by_customer[it.customer].append(it)

    for customer in customers:
        meetings = sorted(by_customer.get(customer, []), key=lambda x: x.dt)
        lines.append(f"## {customer} ({len(meetings)} {'meeting' if len(meetings) == 1 else 'meetings'})")
        lines.append("")
        for it in meetings:
            lines.append(f"### {it.dt.strftime('%a %b %d %H:%M')} — {_md_escape(it.title)}")

            meta: list[str] = []
            if it.primary_source:
                meta.append(f"**Source**: {it.primary_source}")
            if it.opp:
                meta.append(f"**Opp**: {_md_escape(it.opp)}")
            if it.opp_value is not None:
                meta.append(f"**Opp value**: {_fmt_usd(it.opp_value) or str(it.opp_value)}")
            if it.use_cases:
                meta.append(f"**Use cases**: {', '.join(it.use_cases)}")
            if it.notes_source:
                meta.append(f"**Notes source**: {it.notes_source}")
            if meta:
                lines.append("- " + " | ".join(meta))

            if it.customer_attribution:
                lines.append(f"- **Customer attribution**: {it.customer_attribution}")

            link_bits: list[str] = []
            if it.call_url:
                link_bits.append(f"**Call**: `{it.call_url}`")
            if it.transcript_url:
                link_bits.append(f"**Transcript**: `{it.transcript_url}`")
            if it.recording_password:
                link_bits.append(f"**Recording password**: `{it.recording_password}`")
            if link_bits:
                lines.append("- " + " | ".join(link_bits))

            if it.gcal_html_link or it.gcal_attendees:
                gcal_bits: list[str] = []
                if it.gcal_html_link:
                    gcal_bits.append(f"**GCAL**: `{it.gcal_html_link}`")
                if it.gcal_attendees:
                    gcal_bits.append(f"**Attendees**: {', '.join(it.gcal_attendees)}")
                lines.append("- " + " | ".join(gcal_bits))

            if it.summary:
                lines.append("")
                lines.append("**Summary**")
                lines.append("")
                lines.append(_md_escape(it.summary))
            if it.next_steps:
                lines.append("")
                lines.append("**Next steps**")
                lines.append("")
                lines.append(_md_escape(it.next_steps))
            if it.key_points:
                lines.append("")
                lines.append("**Key points**")
                lines.append("")
                lines.append(_md_escape(it.key_points))

            lines.append("")

    if unattributed:
        lines.append(f"## Unattributed ({len(unattributed)} meetings)")
        lines.append("")
        for it in unattributed:
            has_detail = any([it.summary, it.next_steps, it.key_points, it.call_url, it.gcal_attendees])
            if has_detail:
                lines.append(f"### {it.dt.strftime('%a %b %d %H:%M')} — {_md_escape(it.title)}")
                if it.notes_source:
                    lines.append(f"- **Notes source**: {it.notes_source}")
                if it.call_url:
                    lines.append(f"- **Call**: `{it.call_url}`")
                if it.gcal_attendees:
                    lines.append(f"- **Attendees**: {', '.join(it.gcal_attendees)}")
                if it.summary:
                    lines.append("")
                    lines.append("**Summary**")
                    lines.append("")
                    lines.append(_md_escape(it.summary))
                if it.next_steps:
                    lines.append("")
                    lines.append("**Next steps**")
                    lines.append("")
                    lines.append(_md_escape(it.next_steps))
                if it.key_points:
                    lines.append("")
                    lines.append("**Key points**")
                    lines.append("")
                    lines.append(_md_escape(it.key_points))
                lines.append("")
            else:
                lines.append(f"- {it.dt.strftime('%a %b %d %H:%M')} — {_md_escape(it.title)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_ACCOUNT_ALIASES: dict[str, str] = {
    "ssga":            "State Street",
    "ssim":            "State Street",
    "state street global advisors": "State Street",
    "capone":          "Capital One",
    "cap one":         "Capital One",
    "illumina":        "Illumina, Inc",
    "bdl":             "NielsenIQ",
    "nielseniq":       "NielsenIQ",
    "omc":             "Old Mutual Capital",
    "lululemon":       "Lululemon",
    "uhg":             "Optum",
    "unitedhealth":    "Optum",
    "jpmc":            "JPMorgan",
    "jpmorgan chase":  "JPMorgan",
    "jpm":             "JPMorgan",
    "bofa":            "Bank of America",
    "bac":             "Bank of America",
    "gs":              "Goldman Sachs",
    "ms":              "Morgan Stanley",
    "ge":              "General Electric",
    "att":             "AT&T",
}

# Pinned account IDs — bypass DIM_USE_CASE lookup for accounts with multiple
# SF records where we know exactly which one we work with.
# Key: lowercase company name (after alias resolution), Value: (account_id, account_name)
_PINNED_ACCOUNTS: dict[str, tuple[str, str]] = {
    "capital one":          ("0013100001bmAI7AAM", "Capital One Services, LLC"),
    "state street":         ("0013100001qwK6gAAE", "State Street Corporation"),
    "edwards lifesciences": ("0013100001p349cAAA", "Edwards Lifesciences LLC"),
    "abbvie":               ("0010Z00001tG2qeQAC", "Allergan"),
    "nielseniq":            ("001i000001OKhD6AAL", "NielsenIQ"),
    "lululemon":            ("0013100001qwBb0AAE", "Lululemon USA INC"),
    "optum":                ("0013r00002XXvEhAAL", "Optum (UHG)"),
}


def _resolve_missing_account_ids(con, items: list, se_name: str) -> None:
    to_resolve = [
        it for it in items
        if not it.sf_account_id and not it.is_internal
        and (it.gcal_attendees or it.title)
    ]
    if not to_resolve:
        return

    # Step 1: use Cortex to extract the company name from each meeting
    rows = []
    for i, it in enumerate(to_resolve):
        ext_domains = sorted({
            email.split("@")[1].lower()
            for email in (it.gcal_attendees or [])
            if "@" in email and not email.split("@")[1].lower().endswith("snowflake.com")
        })
        domains_str = ", ".join(ext_domains) if ext_domains else "none"
        rows.append((i, f'Title: "{(it.title or "")[:80]}" | External domains: [{domains_str}]'))

    values = ", ".join(f"({i}, %s)" for i, _ in rows)
    params = [text for _, text in rows]
    prompt_prefix = (
        "For each meeting below, identify the CUSTOMER COMPANY NAME. "
        "IMPORTANT: The meeting TITLE is the strongest signal — if the title names a company, use that. "
        "Attendee domains are secondary and should only be used when the title is ambiguous. "
        "Use the full company name (not domain, not abbreviation, not Snowflake, not a person's name). "
        "Output a JSON array of strings in the same order. Use null if Snowflake-internal or unknown.\n\n"
        "Examples:\n"
        '- "CapOne Weekly Call | domains: capitalone.com" → "Capital One"\n'
        '- "DoorDash Sync | domains: doordash.com" → "DoorDash"\n'
        '- "SSIM PoC | domains: ssga.com, statestreet.com" → "State Street"\n'
        '- "SSGA Review | domains: statestreet.com" → "State Street"\n'
        '- "Lululemon SWAT | domains: lululemon.com" → "Lululemon"\n'
        '- "Weekly meeting with AWS | domains: abbvie.com, amazon.com" → "AbbVie"\n'
        '- "BDL Snowflake | domains: nielseniq.com" → "NielsenIQ"\n'
        '- "Summit Dinner <>Edwards Lifesciences<> | domains: dbtlabs.com, edwards.com" → "Edwards Lifesciences"\n'
        '- "UHG CEC Briefing | domains: optum.com" → "Optum"\n'
        '- "Daily Sync - Snowflake X lululemon | domains: lululemon.com" → "Lululemon"\n'
        '- "John Smith - 1 Hour Meeting | domains: snowflake.com" → null\n'
        '- "Internal team meeting | domains: none" → null\n\n'
        "Meetings:\n" + "\n".join(f'{i}: "{text}"' for i, text in rows) + "\n\n"
        "Reply with ONLY the JSON array:"
    )

    company_names: list[str | None] = [None] * len(to_resolve)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-7b', %s)", [prompt_prefix])
            row = cur.fetchone()
            raw = (row[0] or "") if row else ""
        import json as _json
        start, end = raw.find("["), raw.rfind("]") + 1
        if start >= 0 and end > start:
            parsed = _json.loads(raw[start:end])
            for i, name in enumerate(parsed):
                if i >= len(company_names) or not name:
                    continue
                clean = _re.split(r'\s+(?:and|or|,)\s+', str(name).strip(), maxsplit=1)[0].strip()
                # Drop domain-looking values (e.g. "Omc.com", "ssga.com")
                if _re.search(r'\.\w{2,4}$', clean):
                    continue
                if clean.lower() not in ("null", "unknown", "none", ""):
                    company_names[i] = clean
            log.info("  AI extracted %d company names: %s", sum(1 for n in company_names if n), [n for n in company_names if n])
    except Exception as e:
        log.warning("AI company name extraction failed: %s", e)

    # Apply known aliases before account map lookup
    company_names = [_ACCOUNT_ALIASES.get(n.lower(), n) if n else n for n in company_names]

    # Apply pinned accounts — assign known SF IDs directly, skip DIM_USE_CASE lookup
    for i, it in enumerate(to_resolve):
        name = company_names[i]
        if name and name.lower() in _PINNED_ACCOUNTS:
            acct_id, acct_name = _PINNED_ACCOUNTS[name.lower()]
            it.sf_account_id = acct_id
            if it.customer == "(unknown customer)":
                it.customer = acct_name
            log.debug("  Pinned %r → %s (%s)", it.title[:50], acct_name, acct_id)

    # Step 2: batch lookup extracted names against DIM_USE_CASE
    names_to_lookup = {n for n in company_names if n}
    account_map: dict[str, tuple[str, str]] = {}  # company_name_lower -> (account_id, account_name)
    if names_to_lookup:
        patterns = [f"%{n}%" for n in sorted(names_to_lookup)]
        placeholders = ",".join(["%s"] * len(patterns))
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT ACCOUNT_ID, ACCOUNT_NAME
                    FROM MDM.MDM_INTERFACES.DIM_USE_CASE
                    WHERE ACCOUNT_NAME ILIKE ANY ({placeholders})
                      AND IS_LOST = FALSE
                    ORDER BY ACCOUNT_NAME
                    """,
                    patterns,
                )
                for r in cur.fetchall():
                    acct_id, acct_name = str(r[0]).strip(), str(r[1]).strip()
                    if not acct_id:
                        continue
                    # Match each extracted name to the best account
                    for name in names_to_lookup:
                        if name.lower() in acct_name.lower() or acct_name.lower() in name.lower():
                            key = name.lower()
                            existing = account_map.get(key)
                            # Prefer shorter (more specific) account name when multiple match
                            if not existing or len(acct_name) < len(existing[1]):
                                account_map[key] = (acct_id, acct_name)
        except Exception as e:
            log.warning("Account lookup failed: %s", e)

    # Assign results and collect still-unresolved
    still_unresolved = []
    for i, it in enumerate(to_resolve):
        name = company_names[i]
        if name and name.lower() in account_map:
            acct_id, acct_name = account_map[name.lower()]
            it.sf_account_id = acct_id
            if it.customer == "(unknown customer)":
                it.customer = acct_name
            log.debug("  Resolved %r → %s (%s)", it.title, acct_name, acct_id)
        else:
            log.info("  Unresolved: %r → AI said %r (not in account map)", it.title[:50], name)
            still_unresolved.append(it)

    if not still_unresolved:
        return

    # Step 3: look up via Jim's historical SE activities in Salesforce
    # Build search terms: AI-extracted names + significant title phrases for nulls
    _HISTORY_STOPWORDS = {
        "snowflake", "with", "from", "internal", "weekly", "sync", "meeting",
        "call", "update", "review", "session", "strategy", "request", "invitation",
        "interoperability", "platform", "workshop", "enablement", "follow", "recap",
        "databricks", "office", "hours", "team", "check", "planning", "discussion",
        "intro", "demo", "kickoff", "standup", "touchbase", "touch", "base",
        "monday", "tuesday", "wednesday", "thursday", "friday", "next", "steps",
    }

    history_terms: dict[str, list] = {}   # search_pattern -> [MeetingItem, ...]
    # Track which patterns came from keyword fallback (vs AI name) for match strictness
    keyword_patterns: set[str] = set()

    for it in still_unresolved:
        idx = to_resolve.index(it)
        name = company_names[idx]
        if name:
            history_terms.setdefault(f"%{name}%", []).append(it)
        else:
            # Extract consecutive-capitalized phrases (e.g. "Energy Transfer", "State Street")
            # rather than individual words to avoid false matches
            words = _re.findall(r"[A-Z][a-zA-Z]{2,}", it.title or "")
            phrases: list[str] = []
            i = 0
            while i < len(words):
                # Greedily build a phrase of consecutive capitalised words
                phrase_words = [words[i]]
                j = i + 1
                while j < len(words) and words[j][0].isupper():
                    phrase_words.append(words[j])
                    j += 1
                phrase = " ".join(phrase_words)
                # Use phrase if multi-word, or single word that is long and not a stopword
                if len(phrase_words) >= 2:
                    clean = phrase.strip()
                    if not all(w.lower() in _HISTORY_STOPWORDS for w in phrase_words):
                        pat = f"%{clean}%"
                        history_terms.setdefault(pat, []).append(it)
                        keyword_patterns.add(pat)
                elif len(phrase_words) == 1 and len(words[i]) >= 6 and words[i].lower() not in _HISTORY_STOPWORDS:
                    pat = f"%{words[i]}%"
                    history_terms.setdefault(pat, []).append(it)
                    keyword_patterns.add(pat)
                i = j if j > i + 1 else i + 1

    if history_terms:
        log.info("  Checking DIM_SE_ACTIVITY history for %d patterns...", len(history_terms))
        patterns = list(history_terms.keys())
        placeholders = ",".join(["%s"] * len(patterns))
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT ACCOUNT_ID, ACCOUNT_NAME, ACTIVITY_DESCRIPTION,
                           FIRST_VALUE(USE_CASE_ID)  IGNORE NULLS OVER (PARTITION BY ACCOUNT_ID ORDER BY ACTIVITY_DATE DESC) AS LATEST_UC_ID,
                           FIRST_VALUE(OPP_ID)       IGNORE NULLS OVER (PARTITION BY ACCOUNT_ID ORDER BY ACTIVITY_DATE DESC) AS LATEST_OPP_ID,
                           FIRST_VALUE(OPP_NAME)     IGNORE NULLS OVER (PARTITION BY ACCOUNT_ID ORDER BY ACTIVITY_DATE DESC) AS LATEST_OPP_NAME
                    FROM SALES.SE_REPORTING.DIM_SE_ACTIVITY
                    WHERE ACTIVITY_SE_NAME = %s
                      AND (ACCOUNT_NAME ILIKE ANY ({placeholders})
                           OR ACTIVITY_DESCRIPTION ILIKE ANY ({placeholders}))
                      AND ACCOUNT_ID IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY ACCOUNT_ID ORDER BY ACTIVITY_DATE DESC) = 1
                    """,
                    [se_name] + patterns + patterns,
                )
                for r in cur.fetchall():
                    acct_id, acct_name, act_desc = str(r[0]).strip(), str(r[1]).strip(), str(r[2] or "").strip()
                    latest_uc_id  = str(r[3] or "").strip() or None
                    latest_opp_id = str(r[4] or "").strip() or None
                    latest_opp_nm = str(r[5] or "").strip() or None
                    for pattern, matched_items in history_terms.items():
                        term = pattern.strip("%").lower()
                        in_name = term in acct_name.lower()
                        in_desc = term in act_desc.lower()
                        # Keyword fallback patterns: only trust ACCOUNT_NAME matches
                        # (description matches are too loose for generic words/phrases)
                        if pattern in keyword_patterns:
                            if not in_name:
                                continue
                        else:
                            if not (in_name or in_desc):
                                continue
                        for it in matched_items:
                            if not it.sf_account_id:
                                it.sf_account_id = acct_id
                                if it.customer == "(unknown customer)":
                                    it.customer = acct_name
                                # Do NOT populate use case/opp from keyword history —
                                # these are unreliable cross-account matches.
                                # Use case will be resolved via USE_CASE_ATTRIBUTION instead.
                                log.info("  History resolved %r → %s", it.title[:40], acct_name)
        except Exception as e:
            log.warning("Historical activity lookup failed: %s", e)
    sf_emails = {
        email for it in still_unresolved
        for email in (it.gcal_attendees or [])
        if email.endswith("@snowflake.com")
    }
    if not sf_emails:
        return

    ae_accounts: dict[str, list[tuple[str, str]]] = {}
    try:
        ep = ",".join(["%s"] * len(sf_emails))
        with con.cursor() as cur:
            cur.execute(
                f"SELECT SALESFORCE_ACCOUNT_ID, SALESFORCE_OWNER_NAME, REP_EMAIL FROM SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS WHERE REP_EMAIL IN ({ep})",
                list(sf_emails),
            )
            for row in cur.fetchall():
                acct_id, acct_name, rep_email = str(row[0] or "").strip(), str(row[1] or "").strip(), str(row[2] or "").strip()
                if acct_id and rep_email:
                    ae_accounts.setdefault(rep_email, []).append((acct_id, acct_name))
    except Exception as e:
        log.warning("AE fallback lookup failed: %s", e)
        return

    # Use AI-extracted name to pick best AE account
    for i_orig, it in enumerate(still_unresolved):
        name = company_names[to_resolve.index(it)] if it in to_resolve else None
        candidates: list[tuple[str, str]] = []
        for email in (it.gcal_attendees or []):
            if email in ae_accounts:
                candidates.extend(ae_accounts[email])
        if not candidates:
            continue
        if name:
            for acct_id, acct_name in candidates:
                if name.lower() in acct_name.lower() or acct_name.lower() in name.lower():
                    it.sf_account_id = acct_id
                    if it.customer == "(unknown customer)":
                        it.customer = acct_name
                    break





def _ai_classify_meetings(con, items: list) -> None:
    candidates = [
        it for it in items
        if not it.is_internal and not it.summary and not it.sf_opp_id
    ]
    if not candidates:
        return

    log.info("  Running AI_CLASSIFY on %d ambiguous meetings...", len(candidates))
    log.debug("AI candidates: %s", [it.title for it in candidates])

    rows = []
    for i, it in enumerate(candidates):
        ext_domains = sorted({
            email.split("@")[1].lower()
            for email in (it.gcal_attendees or [])
            if "@" in email and not email.split("@")[1].lower().endswith("snowflake.com")
        })
        clean_desc = _re.sub(r"https?://\S+|<[^>]+>|\s+", " ", it.gcal_description or "").strip()[:100]
        attendee_str = f"External attendees: {', '.join(ext_domains)}" if ext_domains else "All attendees at snowflake.com"
        text = f"{(it.title or '')[:80]}. {attendee_str}. {clean_desc}".strip(". ")
        rows.append((i, text))

    values = ", ".join(f"({i}, %s)" for i, _ in rows)
    params = [t for _, t in rows]
    sql = f"""
        SELECT idx, AI_CLASSIFY(text, ARRAY_CONSTRUCT('external', 'internal'))['labels'][0]::string
        FROM (VALUES {values}) t(idx, text)
    """
    try:
        _t = _time.monotonic()
        with con.cursor() as cur:
            cur.execute(sql, params)
            results = {int(row[0]): str(row[1] or "").lower() for row in cur.fetchall()}
        log.info("  AI_CLASSIFY returned in %.1fs", _time.monotonic() - _t)
    except Exception as e:
        log.warning("AI classification failed: %s", e)
        return

    marked = 0
    for i, it in enumerate(candidates):
        if results.get(i, "").startswith("internal"):
            log.debug("  AI filtered: %s", it.title)
            it.is_internal = True
            marked += 1
    log.info("  AI filtered %d non-customer meetings", marked)


def _fetch_tmr_ae_info(con, account_ids: set[str], se_user_id: str | None = None) -> dict[str, dict]:
    if not account_ids:
        return {}
    ids = list(account_ids)
    placeholders = ",".join(["%s"] * len(ids))

    attribution_join = (
        f"JOIN SALES.SE_REPORTING.USE_CASE_ATTRIBUTION a ON a.USE_CASE_ID = d.USE_CASE_ID AND a.USER_ID = '{se_user_id}'"
        if se_user_id else ""
    )

    uc_by_account: dict[str, dict] = {}
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.ACCOUNT_ID, d.USE_CASE_ID, d.USE_CASE_NAME, d.USE_CASE_STAGE,
                       d.STAGE_NUMBER, d.USE_CASE_EACV, d.USE_CASE_LEAD_SE_NAME, d.ACCOUNT_OWNER_NAME
                FROM MDM.MDM_INTERFACES.DIM_USE_CASE d
                {attribution_join}
                WHERE d.ACCOUNT_ID IN ({placeholders})
                  AND d.IS_LOST = FALSE AND d.IS_TECH_WON = FALSE
                  AND d.IS_WON = FALSE AND d.IS_DEPLOYED = FALSE
                  AND d.STAGE_NUMBER BETWEEN 1 AND 5
                ORDER BY d.ACCOUNT_ID, d.STAGE_NUMBER DESC NULLS LAST, d.USE_CASE_EACV DESC NULLS LAST
                """,
                ids,
            )
            cols = [d[0] for d in (cur.description or [])]
            for tup in cur.fetchall():
                row = {cols[i]: tup[i] for i in range(len(cols))}
                acct = str(row["ACCOUNT_ID"] or "").strip()
                if not acct or acct in uc_by_account:
                    continue
                uc_by_account[acct] = {
                    "has_active_uc": True,
                    "uc_id": str(row["USE_CASE_ID"] or "").strip(),
                    "uc_name": str(row["USE_CASE_NAME"] or "").strip(),
                    "uc_stage": str(row["USE_CASE_STAGE"] or "").strip(),
                    "ae_name": str(row["ACCOUNT_OWNER_NAME"] or "").strip() or None,
                    "uc_lead_se_name": str(row["USE_CASE_LEAD_SE_NAME"] or "").strip() or None,
                }
    except Exception as e:
        log.warning("Use case lookup failed: %s", e)

    ae_email_by_account: dict[str, str] = {}
    try:
        with con.cursor() as cur:
            cur.execute(
                f"SELECT SALESFORCE_ACCOUNT_ID, REP_EMAIL FROM SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS WHERE SALESFORCE_ACCOUNT_ID IN ({placeholders})",
                ids,
            )
            for tup in cur.fetchall():
                acct, email = str(tup[0] or "").strip(), str(tup[1] or "").strip()
                if acct and email:
                    ae_email_by_account[acct] = email
    except Exception as e:
        log.warning("AE email lookup failed: %s", e)

    result: dict[str, dict] = {}
    for acct in account_ids:
        uc = uc_by_account.get(acct, {"has_active_uc": False})
        uc["ae_email"] = ae_email_by_account.get(acct)
        result[acct] = uc
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly customer activity report from GCal + Snowhouse.")
    ap.add_argument("--week", metavar="YYYY-MM-DD", help="Monday of the target week (default: last Mon)")
    ap.add_argument("--calendar-id", default="primary")
    ap.add_argument("--internal-domains", default=os.environ.get("INTERNAL_DOMAINS", "snowflake.com"))
    ap.add_argument("--secrets-dir", default=str(HERE / ".secrets"))
    ap.add_argument("--output-dir", default=str(HERE / "output"))
    ap.add_argument("--gsheet", action="store_true", help="Export to CSV with SF account/use case data")
    args = ap.parse_args()

    apply_env(load_env_file(HERE / ".secrets" / "snowhouse.env"), into=os.environ)

    # Resolve who is running the report (used to filter Snowhouse data)
    user_email = _req_env("SNOWHOUSE_SNOWFLAKE_USER")
    # SE display name: read from env or derive from email (e.g. john.doe@... → John Doe)
    se_name = _opt_env("SNOWFLAKE_SE_NAME")
    if not se_name:
        local = user_email.split("@")[0]           # "john.doe"
        se_name = " ".join(p.capitalize() for p in local.split("."))  # "John Doe"
        print(f"  Using derived SE name: {se_name!r}")
        print(f"  If you see no Gong summaries, your Gong display name may differ.")

    # Resolve the SE's Salesforce User ID for USE_CASE_ATTRIBUTION lookup
    _SE_USER_ID: str | None = _opt_env("SNOWFLAKE_SE_USER_ID")
        print(f"  Check a Gong recording to see your exact name, then add to .secrets/snowhouse.env:")
        print(f"    SNOWFLAKE_SE_NAME=Your Gong Name")
    else:
        print(f"  Using SE name from SNOWFLAKE_SE_NAME: {se_name!r}")

    secrets_dir = Path(args.secrets_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve target week (Mon–Sun)
    today = date.today()
    if args.week:
        d = date.fromisoformat(args.week)
        week_start = d - timedelta(days=d.weekday())  # snap to Monday
    else:
        # Last full Mon–Sun week
        days_since_monday = today.weekday()  # 0=Mon
        week_start = today - timedelta(days=days_since_monday + 7)
    week_end = week_start + timedelta(days=6)

    log.info("Reporting week: %s → %s", week_start, week_end)

    now = datetime.now().astimezone()
    tzinfo = now.tzinfo

    log.info("Connecting to Snowflake...")
    _t = _time.monotonic()
    con = _connect_snowflake()
    log.info("Connected in %.1fs", _time.monotonic() - _t)

    # ---- Snowhouse (parameterised SQL) ----
    sql = (HERE / "snowhouse_week.sql").read_text(encoding="utf-8")
    snow_rows: list[tuple[datetime, dict[str, Any]]] = []

    log.info("Querying Snowhouse (Zoom + Gong + SE activity)...")
    _t = _time.monotonic()
    with con.cursor() as cur:
        cur.execute(sql, (user_email, week_start, week_end, se_name, week_start, week_end, se_name, week_start, week_end))
        cols = [d[0] for d in (cur.description or [])]
        for tup in cur:
            row = {cols[i]: tup[i] for i in range(len(cols))}
            dt = row.get("MEETING_DATETIME")
            if isinstance(dt, datetime):
                dt = dt if dt.tzinfo else dt.replace(tzinfo=tzinfo)
            else:
                continue
            snow_rows.append((dt, row))

    all_se_accounts = fetch_all_se_accounts(con, se_name)
    log.info("Snowhouse query done in %.1fs — %d raw rows", _time.monotonic() - _t, len(snow_rows))

    snow_rows = _dedupe(snow_rows, tzinfo)
    rows_only = [r for _, r in snow_rows]
    preferred = CustomerAttributor.build_preferred_accounts(rows_only)
    known = CustomerAttributor.build_known_accounts(rows_only) | all_se_accounts
    attributor = CustomerAttributor(known, preferred_accounts=preferred | all_se_accounts)

    items: list[MeetingItem] = []
    for dt, row in snow_rows:
        title = _pick_title(row)
        customer, badge = _resolve_customer(row, title, attributor, preferred)
        items.append(MeetingItem(
            dt=dt,
            customer=customer,
            title=title,
            customer_attribution=badge,
            sources={"Snowhouse"},
            is_internal=is_internal_call(customer if customer != "(unknown customer)" else None, title),
            opp=_maybe_str(row, "OPP_NAME"),
            sf_account_id=_maybe_str(row, "SF_ACCOUNT_ID"),
            sf_activity_id=_maybe_str(row, "SF_ACTIVITY_ID"),
            sf_opp_id=_maybe_str(row, "SF_OPP_ID"),
            sf_use_case_id=_maybe_str(row, "SF_USE_CASE_ID"),
            se_name=_maybe_str(row, "SE_NAME"),
            se_email=(_maybe_str(row, "SE_HIERARCHY_EMAIL") or "").split(",")[-1].strip() or None,
            summary=_maybe_str(row, "SUMMARY"),
            next_steps=_maybe_str(row, "NEXT_STEPS"),
            key_points=_maybe_str(row, "KEY_POINTS"),
            call_url=_maybe_str(row, "CALL_URL"),
            transcript_url=_maybe_str(row, "TRANSCRIPT_URL"),
            recording_password=_maybe_str(row, "RECORDING_PASSWORD"),
            notes_source=_maybe_str(row, "SUMMARY_SOURCE"),
            primary_source=_maybe_str(row, "PRIMARY_SOURCE"),
        ))

    # ---- Google Calendar ----
    auth = GoogleCalendarAuth(
        credentials_json=secrets_dir / "google_credentials.json",
        token_json=secrets_dir / "google_token.json",
    )
    log.info("Fetching Google Calendar events...")
    _t = _time.monotonic()
    service = load_google_calendar_service(auth)
    time_min = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    time_max = datetime.combine(week_end + timedelta(days=1), time.min, tzinfo=timezone.utc)

    internal_domains = tuple(d.strip().lower() for d in args.internal_domains.split(",") if d.strip())
    heuristics = CustomerMeetingHeuristics(internal_domains=internal_domains)

    gcal_count = 0
    for ev in iter_events(service=service, calendar_id=args.calendar_id, time_min=time_min, time_max=time_max):
        gcal_count += 1
        if ev.get("status") == "cancelled" or not ev.get("attendees"):
            continue
        m = event_to_meeting(event=ev, calendar_id=args.calendar_id, tzinfo=tzinfo)
        g_title = m.summary or "(untitled meeting)"
        idx = _match_gcal(g_title, m.start, m.conference_url, items)
        if idx is not None:
            it = items[idx]
            it.sources.add("Google Calendar")
            it.gcal_html_link = m.html_link
            it.gcal_conference_url = m.conference_url
            it.gcal_attendees = [a.email for a in m.attendees[:15]]
            it.gcal_description = (m.description or "")[:500] or None
            continue

        if not is_customer_meeting(m, heuristics):
            continue
        ctx = _md_escape(m.description or "")
        res = attributor.attribute(title=g_title, context=ctx)
        cust, badge = "(unknown customer)", None
        if res.customer and res.confidence >= 0.75:
            cust = canonicalize_customer(res.customer, preferred_accounts=preferred) or "(unknown customer)"
            badge = "Inferred (high)" if res.confidence >= 0.85 else "Inferred (medium)"

        items.append(MeetingItem(
            dt=m.start,
            customer=cust,
            title=g_title,
            customer_attribution=badge,
            sources={"Google Calendar"},
            is_internal=is_internal_call(cust if cust != "(unknown customer)" else None, g_title),
            gcal_html_link=m.html_link,
            gcal_conference_url=m.conference_url,
            gcal_attendees=[a.email for a in m.attendees[:15]],
            gcal_description=(m.description or "")[:500] or None,
        ))

    log.info("Google Calendar done in %.1fs — %d events, %d items total", _time.monotonic() - _t, gcal_count, len(items))

    # ---- Domain-based attribution ----
    log.info("Running domain-based attribution...")
    _t = _time.monotonic()
    for it in items:
        if it.customer != "(unknown customer)":
            continue
        if not it.gcal_attendees:
            continue
        res = attributor.attribute_from_domains(it.gcal_attendees, internal_domains)
        if res.customer and res.confidence >= 0.75:
            it.customer = canonicalize_customer(res.customer, preferred_accounts=preferred) or "(unknown customer)"
            it.customer_attribution = "Domain match"
            it.is_internal = is_internal_call(
                it.customer if it.customer != "(unknown customer)" else None, it.title
            )

    # ---- Web-lookup attribution for still-unattributed meetings ----
    resolver = DomainResolver(cache_path=secrets_dir / "domain_cache.json")
    for it in items:
        if it.customer != "(unknown customer)":
            continue
        if not it.gcal_attendees:
            continue
        for email in it.gcal_attendees:
            if "@" not in email:
                continue
            domain = email.split("@", 1)[1].lower()
            if any(domain == d or domain.endswith("." + d) for d in internal_domains):
                continue
            company = resolver.resolve(domain)
            if not company:
                continue
            res = attributor.attribute(title=company, context="")
            if res.customer and res.confidence >= 0.70:
                it.customer = canonicalize_customer(res.customer, preferred_accounts=preferred | all_se_accounts) or "(unknown customer)"
                it.customer_attribution = "Domain lookup"
                it.is_internal = is_internal_call(
                    it.customer if it.customer != "(unknown customer)" else None, it.title
                )
                break
    resolver.save_cache()
    log.info("Domain attribution done in %.1fs", _time.monotonic() - _t)

    # ---- Salesforce use cases (direct MDM query) ----
    log.info("Looking up Salesforce use cases...")
    _t = _time.monotonic()
    opp_ids = {it.sf_opp_id for it in items if it.sf_opp_id}
    opp_names_set = {it.opp for it in items if it.opp}
    if opp_ids or opp_names_set:
        try:
            all_ids = list(opp_ids)
            all_names = list(opp_names_set)
            id_ph = ",".join(["%s"] * len(all_ids)) if all_ids else "NULL"
            nm_ph = ",".join(["%s"] * len(all_names)) if all_names else "NULL"
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT OPP_ID, OPPORTUNITY_NAME, USE_CASE_NAME, USE_CASE_EACV
                    FROM MDM.MDM_INTERFACES.DIM_USE_CASE
                    WHERE (OPP_ID IN ({id_ph}) OR OPPORTUNITY_NAME IN ({nm_ph}))
                      AND IS_LOST = FALSE
                    ORDER BY USE_CASE_EACV DESC NULLS LAST
                    """,
                    all_ids + all_names,
                )
                uc_by_opp: dict[str, list[str]] = {}
                val_by_opp: dict[str, float] = {}
                for row in cur.fetchall():
                    opp_id, opp_nm, uc_name, eacv = row
                    key = str(opp_id or opp_nm or "").strip()
                    if not key:
                        continue
                    if uc_name:
                        uc_by_opp.setdefault(key, []).append(str(uc_name))
                    if eacv is not None:
                        val_by_opp[key] = max(val_by_opp.get(key, 0.0), float(eacv))
            for it in items:
                key = it.sf_opp_id or it.opp or ""
                if key and key in uc_by_opp:
                    it.use_cases = _uniq(uc_by_opp[key])
                    it.opp_value = val_by_opp.get(key)
        except Exception as e:
            log.warning("Use case lookup failed: %s", e)
    log.info("Salesforce use cases done in %.1fs", _time.monotonic() - _t)

    con.close()

    # ---- Write output ----
    log.info("Rendering markdown report...")
    md = _render(items, week_start, week_end)
    out_path = out_dir / f"week_{week_start}.md"
    out_path.write_text(md, encoding="utf-8")
    log.info("Wrote %s (%d meetings, %d customers)", out_path, len(items), len({it.customer for it in items if it.customer != "(unknown customer)"}))

    gong_count = sum(1 for it in items if it.notes_source in ("Gong", "Zoom") or it.summary)
    if gong_count == 0 and items:
        log.warning("No Gong/Zoom summaries — SE name %r may not match Gong display name. Set SNOWFLAKE_SE_NAME in .secrets/snowhouse.env", se_name)

    if args.gsheet:
        log.info("--- CSV export ---")
        with _connect_snowflake() as con_gsheet:
            log.info("Pass 1: resolving missing account IDs...")
            _t = _time.monotonic()
            _resolve_missing_account_ids(con_gsheet, items, se_name)
            log.info("Account ID resolution done in %.1fs", _time.monotonic() - _t)

            _t = _time.monotonic()
            pre = sum(1 for it in items if it.is_internal)
            for it in items:
                if it.is_internal or not it.gcal_attendees:
                    continue
                all_internal = all(
                    a.lower().endswith("@snowflake.com") or a.startswith("c_") or "@" not in a
                    for a in it.gcal_attendees
                )
                if all_internal and not it.summary and not it.sf_opp_id:
                    it.is_internal = True
            log.info("All-internal attendee filter: removed %d in %.1fs", sum(1 for it in items if it.is_internal) - pre, _time.monotonic() - _t)

            log.info("Pass 2: AI_CLASSIFY filter...")
            _t = _time.monotonic()
            _ai_classify_meetings(con_gsheet, items)
            log.info("AI classification done in %.1fs", _time.monotonic() - _t)

            account_ids = {it.sf_account_id for it in items if it.sf_account_id and not it.is_internal}
            tmr_ae: dict[str, dict] = {}
            if account_ids:
                log.info("Pass 3: use case / AE lookup for %d accounts...", len(account_ids))
                _t = _time.monotonic()
                tmr_ae = _fetch_tmr_ae_info(con_gsheet, account_ids, se_user_id=_SE_USER_ID)
                log.info("Use case / AE lookup done in %.1fs", _time.monotonic() - _t)

        def _has_external_attendee(it: MeetingItem) -> bool:
            return any(
                not a.lower().endswith("@snowflake.com") and "@" in a
                for a in (it.gcal_attendees or [])
            )

        sheet_rows = [
            {
                "dt": it.dt,
                "title": it.title,
                "customer": it.customer if it.customer != "(unknown customer)" else "",
                "source": it.primary_source or ", ".join(sorted(it.sources)),
                "sf_account_id": it.sf_account_id or "",
                "sf_activity_id": it.sf_activity_id or "",
                "opp": it.opp or "",
                "sf_opp_id": it.sf_opp_id or "",
                "sf_use_case_id": (it.sf_use_case_id if it.sf_activity_id else "") or "",
                "use_cases": it.use_cases or [],
                "summary": it.summary or "",
                "next_steps": it.next_steps or "",
                "attendees": it.gcal_attendees or [],
                "call_url": it.call_url or "",
                "gcal_html_link": it.gcal_html_link or "",
                "se_name": it.se_name or "",
                "se_email": it.se_email or "",
                **tmr_ae.get(it.sf_account_id or "", {}),
            }
            for it in sorted(items, key=lambda x: x.dt)
            if not it.is_internal
            and (it.customer != "(unknown customer)" or it.sf_account_id or _has_external_attendee(it) or it.summary)
        ]
        _t = _time.monotonic()
        csv_path = out_dir / f"week_{week_start}.csv"
        write_weekly_csv(csv_path, week_start, week_end, sheet_rows)
        log.info("Wrote %s (%d rows) in %.1fs", csv_path, len(sheet_rows), _time.monotonic() - _t)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
