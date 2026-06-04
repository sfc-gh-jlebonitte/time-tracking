from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MeetingAttendee:
    email: str
    response_status: str | None = None
    is_organizer: bool | None = None
    is_self: bool | None = None
    optional: bool | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class CalendarMeeting:
    provider: str  # e.g. "google"
    calendar_id: str
    event_id: str
    summary: str | None
    start: datetime
    end: datetime
    organizer_email: str | None = None
    attendees: tuple[MeetingAttendee, ...] = ()
    location: str | None = None
    conference_url: str | None = None
    html_link: str | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MeetingNote:
    source: str  # e.g. "snowhouse"
    note_id: str
    title: str | None
    body: str | None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MeetingWithNotes:
    meeting: CalendarMeeting
    notes: tuple[MeetingNote, ...]
