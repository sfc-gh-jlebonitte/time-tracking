from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable

from dateutil import parser as date_parser
import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .models import CalendarMeeting, MeetingAttendee


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


@dataclass(frozen=True)
class GoogleCalendarAuth:
    credentials_json: Path
    token_json: Path


def _parse_event_dt(dt_obj: dict[str, Any], tzinfo) -> datetime:
    if "dateTime" in dt_obj and dt_obj["dateTime"]:
        return date_parser.isoparse(dt_obj["dateTime"]).astimezone(tzinfo)
    if "date" in dt_obj and dt_obj["date"]:
        d = date_parser.isoparse(dt_obj["date"]).date()
        return datetime.combine(d, time.min, tzinfo=tzinfo)
    raise ValueError(f"Unsupported event datetime payload: {dt_obj}")


def _extract_conference_url(event: dict[str, Any]) -> str | None:
    for key in ("hangoutLink",):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    conf = event.get("conferenceData") or {}
    entry_points = conf.get("entryPoints") or []
    if isinstance(entry_points, list):
        for ep in entry_points:
            if not isinstance(ep, dict):
                continue
            uri = ep.get("uri")
            if isinstance(uri, str) and uri.strip():
                return uri.strip()
    return None


def load_google_calendar_service(auth: GoogleCalendarAuth):
    creds: Credentials | None = None
    if auth.token_json.exists():
        creds = Credentials.from_authorized_user_file(str(auth.token_json), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        # Prefer explicit OAuth client secrets if present.
        if auth.credentials_json.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(auth.credentials_json), SCOPES)
            creds = flow.run_local_server(port=0)
            auth.token_json.parent.mkdir(parents=True, exist_ok=True)
            auth.token_json.write_text(creds.to_json(), encoding="utf-8")
        else:
            # Fallback to Application Default Credentials (e.g. `gcloud auth application-default login`).
            adc_creds, _ = google.auth.default(scopes=SCOPES)
            if getattr(adc_creds, "expired", False):
                adc_creds.refresh(Request())
            creds = adc_creds  # type: ignore[assignment]

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def iter_events(
    *,
    service,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
    max_results_per_page: int = 2500,
) -> Iterable[dict[str, Any]]:
    if time_min.tzinfo is None or time_max.tzinfo is None:
        raise ValueError("time_min/time_max must be timezone-aware")

    page_token: str | None = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results_per_page,
                pageToken=page_token,
            )
            .execute()
        )

        items = resp.get("items") or []
        for ev in items:
            if isinstance(ev, dict):
                yield ev

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def event_to_meeting(*, event: dict[str, Any], calendar_id: str, tzinfo) -> CalendarMeeting:
    start = _parse_event_dt(event.get("start") or {}, tzinfo)
    end = _parse_event_dt(event.get("end") or {}, tzinfo)

    attendees: list[MeetingAttendee] = []
    for a in (event.get("attendees") or []):
        if not isinstance(a, dict):
            continue
        email = a.get("email")
        if not isinstance(email, str) or not email.strip():
            continue
        attendees.append(
            MeetingAttendee(
                email=email.strip().lower(),
                response_status=a.get("responseStatus"),
                is_organizer=a.get("organizer"),
                is_self=a.get("self"),
                optional=a.get("optional"),
                display_name=a.get("displayName"),
            )
        )

    organizer = event.get("organizer") or {}
    organizer_email = organizer.get("email") if isinstance(organizer, dict) else None
    if isinstance(organizer_email, str):
        organizer_email = organizer_email.strip().lower()
    else:
        organizer_email = None

    return CalendarMeeting(
        provider="google",
        calendar_id=calendar_id,
        event_id=str(event.get("id") or ""),
        summary=event.get("summary"),
        start=start,
        end=end,
        organizer_email=organizer_email,
        attendees=tuple(attendees),
        location=event.get("location"),
        conference_url=_extract_conference_url(event),
        html_link=event.get("htmlLink"),
        description=event.get("description"),
        raw=event,
    )
