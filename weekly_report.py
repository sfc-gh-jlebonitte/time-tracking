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
import os
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
from lib.salesforce_use_cases import fetch_salesforce_use_cases_for_opps

HERE = Path(__file__).resolve().parent


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
            lines.append(f"- {it.dt.strftime('%a %b %d %H:%M')} — {_md_escape(it.title)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly customer activity report from GCal + Snowhouse.")
    ap.add_argument("--week", metavar="YYYY-MM-DD", help="Monday of the target week (default: last Mon)")
    ap.add_argument("--calendar-id", default="primary")
    ap.add_argument("--internal-domains", default=os.environ.get("INTERNAL_DOMAINS", "snowflake.com"))
    ap.add_argument("--secrets-dir", default=str(HERE / ".secrets"))
    ap.add_argument("--output-dir", default=str(HERE / "output"))
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
        week_start = date.fromisoformat(args.week)
    else:
        # Last full Mon–Sun week
        days_since_monday = today.weekday()  # 0=Mon
        week_start = today - timedelta(days=days_since_monday + 7)
    week_end = week_start + timedelta(days=6)

    print(f"Reporting week: {week_start} → {week_end}")

    now = datetime.now().astimezone()
    tzinfo = now.tzinfo

    # ---- Snowhouse (parameterised SQL) ----
    sql = (HERE / "snowhouse_week.sql").read_text(encoding="utf-8")
    snow_rows: list[tuple[datetime, dict[str, Any]]] = []

    with _connect_snowflake() as con:
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
    service = load_google_calendar_service(auth)
    time_min = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    time_max = datetime.combine(week_end + timedelta(days=1), time.min, tzinfo=timezone.utc)

    internal_domains = tuple(d.strip().lower() for d in args.internal_domains.split(",") if d.strip())
    heuristics = CustomerMeetingHeuristics(internal_domains=internal_domains)

    for ev in iter_events(service=service, calendar_id=args.calendar_id, time_min=time_min, time_max=time_max):
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
        ))

    # ---- Domain-based attribution for unattributed meetings ----
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

    # ---- Salesforce use cases ----
    opp_names: set[str] = {it.opp for it in items if it.opp}
    if opp_names:
        with _connect_snowflake() as con_sf:
            with con_sf.cursor() as cur_db:
                cur_db.execute("select current_database()")
                row = cur_db.fetchone()
                cur_db_name = str(row[0]).strip() if row and row[0] else ""
        with _connect_snowflake() as con_sf:
            uc_rows, _ = fetch_salesforce_use_cases_for_opps(
                con=con_sf,
                opp_ids=set(),
                opp_names={s for s in opp_names if s.strip()},
                databases=_uniq([cur_db_name, "SALES"]),
            )
        alias_to_canon: dict[str, str] = {}
        canon_use_cases: dict[str, list[str]] = {}
        canon_amount: dict[str, float | None] = {}
        for r in uc_rows:
            canon = (r.opp_id or r.opp_name or "").strip()
            if not canon:
                continue
            if r.opp_name:
                alias_to_canon[r.opp_name.strip()] = canon
            canon_use_cases.setdefault(canon, []).append(r.use_case)
            if r.amount is not None:
                prev = canon_amount.get(canon)
                canon_amount[canon] = max(prev or 0.0, float(r.amount))
            else:
                canon_amount.setdefault(canon, None)
        for k in list(canon_use_cases.keys()):
            canon_use_cases[k] = _uniq(canon_use_cases[k])
        for it in items:
            if it.opp:
                canon = alias_to_canon.get(it.opp.strip())
                if canon:
                    it.use_cases = canon_use_cases.get(canon)
                    it.opp_value = canon_amount.get(canon)

    # ---- Write output ----
    md = _render(items, week_start, week_end)
    out_path = out_dir / f"week_{week_start}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"  {len(items)} meetings, {len({it.customer for it in items if it.customer != '(unknown customer)'})} customers")

    gong_count = sum(1 for it in items if it.notes_source in ("Gong", "Zoom") or it.summary)
    if gong_count == 0 and items:
        print()
        print("  NOTE: No Gong/Zoom summaries found. This usually means the SE name used")
        print(f"  to query Gong ({se_name!r}) does not match your Gong display name.")
        print("  Open a recent Gong recording, check how your name appears in the")
        print("  participants list, then add to .secrets/snowhouse.env:")
        print("    SNOWFLAKE_SE_NAME=Your Exact Gong Name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
