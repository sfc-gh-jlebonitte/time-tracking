from __future__ import annotations

from dataclasses import dataclass

from .models import CalendarMeeting


@dataclass(frozen=True)
class CustomerMeetingHeuristics:
    internal_domains: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = (
        "lunch",
        "ooo",
        "out of office",
        "hold",
        "focus time",
    )


def _email_domain(email: str) -> str | None:
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].lower().strip()


def is_customer_meeting(meeting: CalendarMeeting, heuristics: CustomerMeetingHeuristics) -> bool:
    title = (meeting.summary or "").strip().lower()
    for kw in heuristics.exclude_keywords:
        if kw in title:
            return False

    internal = {d.lower().strip() for d in heuristics.internal_domains if d.strip()}
    if not internal:
        # With no internal domain configured, default to "customer meeting" only
        # when there is at least one attendee email present (i.e., not a solo block).
        return any(a.email for a in meeting.attendees)

    all_emails = [a.email for a in meeting.attendees if a.email]
    if meeting.organizer_email:
        all_emails.append(meeting.organizer_email)

    external_domains = set()
    for e in all_emails:
        dom = _email_domain(e)
        if dom and dom not in internal:
            external_domains.add(dom)

    return len(external_domains) > 0


def guess_customer_domains(meeting: CalendarMeeting, internal_domains: tuple[str, ...]) -> tuple[str, ...]:
    internal = {d.lower().strip() for d in internal_domains if d.strip()}
    domains: set[str] = set()
    for a in meeting.attendees:
        dom = _email_domain(a.email)
        if dom and (not internal or dom not in internal):
            domains.add(dom)
    return tuple(sorted(domains))
