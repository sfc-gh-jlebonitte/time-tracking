# lib — inlined dependencies for the weekly time tracking report
#
# These modules are copied from the collect-notes project so that this skill
# is self-contained and requires no external private packages.
#
# Source: https://github.com/your-org/collect-notes  (internal)
# Modules included:
#   models.py              — core data classes (CalendarMeeting, MeetingAttendee, etc.)
#   calendar_google.py     — Google Calendar OAuth auth + event fetching
#   customer_attribution.py — deterministic customer name resolution
#   customer_meetings.py   — heuristics for identifying customer vs internal meetings
#   internal_calls.py      — known-internal meeting title patterns
#   salesforce_use_cases.py — Salesforce use-case discovery + fetching from Snowhouse
#   envfile.py             — minimal .env file loader
