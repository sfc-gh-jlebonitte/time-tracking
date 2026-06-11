# Data Flow — Weekly Activity Report

## Overview

The pipeline merges activity data from three real-time sources (Zoom, Gong, Salesforce SE Activity) with Google Calendar, then enriches each meeting with Salesforce account, opportunity, and use case context. The output is a markdown report for the SE and a CSV for the use case team.

```
Google Calendar ──────────────────────────────────┐
Zoom (IT.RAW_ZOOM_CUSTOM)  ──┐                    │
Gong (GONG_SHARE)  ──────────┤── Snowhouse SQL ───┤── Merge & Dedupe ──► MeetingItem list
SF SE Activity (DIM_SE_ACTIVITY) ─────────────────┘                           │
                                                                               ▼
                                                              Account ID Resolution (3-pass)
                                                                               │
                                                                               ▼
                                                              Meeting Filter Pipeline
                                                                               │
                                                             ┌─────────────────┴──────────────┐
                                                             ▼                                ▼
                                                     Markdown Report                     CSV Export
                                                  (output/week_*.md)              (output/week_*.csv)
```

---

## Source 1 — Zoom (`IT.RAW_ZOOM_CUSTOM`)

| Table | Key columns used |
|---|---|
| `ZOOM_MEETINGS` | `ID`, `TOPIC`, `HOST`, `START_TIME` |
| `ZOOM_MEETING_PARTICIPANTS` | `MEETING_ID`, `EMAIL` |

**Filter:** Meetings where `p.EMAIL = <user_email>` and `START_TIME` is within the target week.

**Output:** One row per Zoom meeting the SE attended, with meeting ID, title, host, and start time (stored as UTC TIMESTAMP_NTZ).

---

## Source 2 — Gong (`GONG_SHARE.GONG_DATA_CLOUD`)

| Table | Key columns used |
|---|---|
| `CALLS` | `CONVERSATION_KEY`, `TITLE`, `EFFECTIVE_START_DATETIME`, `CALL_SPOTLIGHT_BRIEF`, `CALL_SPOTLIGHT_NEXT_STEPS`, `CALL_SPOTLIGHT_KEY_POINTS`, `CALL_URL` |
| `CONVERSATION_PARTICIPANTS` | `CONVERSATION_KEY`, `NAME` |

**Filter:** Calls where `cp.NAME = <SE display name>` (e.g. `Jim Lebonitte`) and `EFFECTIVE_START_DATETIME` is within the target week.

**Business logic:** Gong stores times as TIMESTAMP_TZ (UTC). When cast to TIMESTAMP_NTZ for the UNION ALL, the UTC value is preserved numerically but loses its timezone tag. Python then applies the local timezone to align with Zoom (which stores local wall-clock time as NTZ).

**Output:** Gong call metadata including AI-generated spotlight brief (summary), next steps, key points, and call URL.

---

## Source 3 — Salesforce SE Activity (`SALES.SE_REPORTING.DIM_SE_ACTIVITY`)

**Filter:** `ACTIVITY_SE_NAME = <SE name>`, `ACTIVITY_TYPE = 'Meeting'`, `ACTIVITY_DATE` within the target week.

**Key columns:** `ACTIVITY_ID`, `ACTIVITY_DATE`, `ACTIVITY_DESCRIPTION`, `ACCOUNT_ID`, `ACCOUNT_NAME`, `OPP_ID`, `OPP_NAME`, `USE_CASE_ID`, `MEETING_STATUS`, `ACTIVITY_SE_HIERARCHY_EMAIL`

**Business logic:** This is the authoritative Salesforce source for linking a meeting to an account, opportunity, and use case. The `ACTIVITY_DESCRIPTION` mirrors the meeting title as logged by the SE. `ACTIVITY_SE_HIERARCHY_EMAIL` is a comma-separated chain from SVP down to the SE; the SE's own email is the last element.

**Output:** Account/opportunity/use case IDs for meetings the SE formally logged in Salesforce.

---

## Snowhouse SQL — Merge Logic (`snowhouse_week.sql`)

The three sources are merged into a unified meeting list via CTEs:

```
zoom_meetings  ──────────────────────────────────────────┐
                                                          ├── all_meetings (UNION ALL)
gong_unmatched (Gong calls with no Zoom match ±20 min) ──┤
                                                          │
se_unmatched   (SF activities with no Zoom/Gong match) ──┘
```

**Matching rules:**
- A Gong call is considered matched to a Zoom meeting if they share the same date and start within 20 minutes of each other (`ABS(DATEDIFF('minute', ...)) <= 20`).
- An SE activity is considered matched if it shares the same date and the Zoom meeting title contains the activity description's first segment (split on ` - `).
- Unmatched Gong/SE records are included as standalone meetings.

The final SELECT joins `all_meetings` back to `se_activities` (for SF IDs) and `gong_calls` (for summaries).

---

## Source 4 — Google Calendar (`calendar_google.py`)

**API:** Google Calendar v3 (`events.list`), scoped to `calendar.readonly`.

**Filter:** All events in the target week from the primary calendar that have attendees and are not cancelled.

**Business logic:**
1. For each GCal event, attempt to match to an existing Snowhouse item by title Jaccard similarity (≥0.25) and time proximity (±20 min). If matched, enrich the existing item with attendees, GCal link, and conference URL.
2. If no match and the event has at least one external (non-Snowflake) attendee, create a new GCal-only `MeetingItem`.
3. Customer attribution runs on unmatched GCal items via `CustomerAttributor`, domain-based matching (`DomainResolver`), and known SE accounts (`fetch_all_se_accounts`).

---

## Account ID Resolution — 3-Pass Pipeline

Runs during CSV export for items where `sf_account_id` is still blank.

### Pass 1 — AI Company Name Extraction + DIM_USE_CASE Lookup

1. For each unresolved meeting, build a text string: `"Title | External domains: [x.com, y.com]"`.
2. Batch call to `SNOWFLAKE.CORTEX.COMPLETE('mistral-7b')` with few-shot examples to extract the customer company name (e.g. `"CapOne Weekly | capitalone.com"` → `"Capital One"`).
3. Batch `ILIKE ANY` lookup against `MDM.MDM_INTERFACES.DIM_USE_CASE` using the extracted names. Picks the shortest matching account name (most specific).

**Source table:** `MDM.MDM_INTERFACES.DIM_USE_CASE`
**Filter:** `IS_LOST = FALSE`

### Pass 2 — Historical SE Activity Lookup

For items still unresolved (AI returned null or no DIM_USE_CASE match):

1. Build search patterns from AI-extracted names + significant title keywords.
2. Query `SALES.SE_REPORTING.DIM_SE_ACTIVITY` for the SE's historical meetings matching by `ACCOUNT_NAME ILIKE ANY (...)` or `ACTIVITY_DESCRIPTION ILIKE ANY (...)`.
3. Uses `FIRST_VALUE(USE_CASE_ID) IGNORE NULLS OVER (PARTITION BY ACCOUNT_ID ORDER BY ACTIVITY_DATE DESC)` to pull the **most recently updated** use case for that account.
4. Assigns `sf_account_id`, `sf_use_case_id`, `sf_opp_id`, and `sf_activity_id` from the best match.

**Business logic:** This ensures that if the SE has updated a use case on a past activity, the latest value is always used rather than a stale one.

### Pass 3 — AE Attendee Fallback

For any remaining unresolved items:

1. Extract all `@snowflake.com` attendees from the meeting.
2. Look up which of those are AE owners via `SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS.REP_EMAIL`.
3. Cross-reference the AE's account list with the AI-extracted company name.

---

## Meeting Filter Pipeline

Applied before writing the CSV to remove non-customer-facing meetings:

| Filter | Logic |
|---|---|
| **Title patterns** | 20+ substrings: `1:1`, `touchbase`, `recruiting`, `hiring manager`, `breathe & stretch`, `enablement`, `industry principles`, etc. (`lib/internal_calls.py`) |
| **All-internal attendees** | All attendees are `@snowflake.com` + no Gong summary + no opp ID → internal |
| **AI_CLASSIFY** | Batched `AI_CLASSIFY(text, ['external', 'internal'])` SQL call using meeting title, external domains, and GCal description. Only runs on meetings with no Gong summary and no opp ID. |
| **External attendee check** | Final CSV gate: must have a known account, external attendee, or Gong summary to be included. |

---

## Active Use Case Lookup (`MDM.MDM_INTERFACES.DIM_USE_CASE`)

Runs once per CSV export for all resolved account IDs.

**Filter:** `IS_LOST = FALSE`, `STAGE_NUMBER BETWEEN 1 AND 6` (active, not yet deployed).

**Selection:** Highest `STAGE_NUMBER`, then highest `USE_CASE_EACV` as tiebreaker — one best use case per account.

**Output fields:** `uc_id`, `uc_name`, `uc_stage`, `ae_name` (account owner), `uc_lead_se_name`.

**AE email** is supplemented from `SALES.RAVEN.D_SALESFORCE_ACCOUNT_CUSTOMERS.REP_EMAIL`.

---

## CSV Output Schema

| Column | Source | Notes |
|---|---|---|
| Date / Day / Time | `MeetingItem.dt` | Local wall-clock time |
| Meeting Title | Zoom topic / Gong title / GCal summary | Priority: Zoom > Gong > SE Activity > GCal |
| Customer | `CustomerAttributor` | Inferred from attendees, domains, or SF account name |
| Source | `PRIMARY_SOURCE` | Zoom / Gong / SE Activity / Google Calendar |
| SF Account ID | `DIM_SE_ACTIVITY` or resolution pipeline | Salesforce Account object ID |
| SF Activity ID | `DIM_SE_ACTIVITY.ACTIVITY_ID` | Blank if meeting not yet logged in SF; use for upsert/merge |
| Use Case Tagged in SF | Derived | Yes / No / Unknown (Unknown = account not resolved) |
| Opportunity Name / ID | `DIM_SE_ACTIVITY` or `DIM_USE_CASE` | |
| Use Case Name / ID | `MDM.MDM_INTERFACES.DIM_USE_CASE` | Best active use case for the account |
| AE Name / Email | `D_SALESFORCE_ACCOUNT_CUSTOMERS` | Only populated when no active use case found |
| Use Case Lead SE | `DIM_USE_CASE.USE_CASE_LEAD_SE_NAME` | |
| Meeting SE Name / Email | `DIM_SE_ACTIVITY.ACTIVITY_SE_HIERARCHY_EMAIL` | Last element of hierarchy chain = SE's own email |
| Summary / Next Steps | Gong `CALL_SPOTLIGHT_BRIEF` / `CALL_SPOTLIGHT_NEXT_STEPS` | Truncated to 2000 chars |
| Attendees | Google Calendar attendees | Up to 15, `@` format |
| Call URL | `GONG_URL` | |
| GCal Link | Google Calendar event URL | |

---

## Key Design Decisions

- **Single Snowflake connection** — one connection is opened at the start of each run and reused for all queries to avoid repeated warehouse resume overhead.
- **Gong as primary summary source** — Zoom transcript join (`IT.ZOOM_TRANSCRIPT.ZOOM_MEETING_SUMMARY_VW`) and recording URL lookup (`ZOOM_MEETING_RECORDINGS_RAW` LATERAL FLATTEN) are excluded from the default query due to high latency; Gong spotlight briefs are the primary summary source.
- **AI company extraction over hardcoded rules** — `SNOWFLAKE.CORTEX.COMPLETE` with few-shot examples handles abbreviations (`CapOne`, `SSIM`), compound domains (`capitalone.com`), and edge cases without maintaining a lookup table.
- **Most-recent use case wins** — `FIRST_VALUE(...) IGNORE NULLS OVER (...ORDER BY ACTIVITY_DATE DESC)` ensures the historical lookup always reflects the SE's latest updates to a use case, not a stale one from an older activity.
- **SF Activity ID as merge key** — when a meeting has a corresponding logged SF activity, the `ACTIVITY_ID` is included so downstream systems can upsert/merge records without creating duplicates.
