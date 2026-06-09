from __future__ import annotations

import re

_INTERNAL_CUSTOMERS_NORM = {
    "balancing work and motherhood",
    "ci office hours",
    "coco",
    "cortex code office hours",
    "creating space for success",
    "de product roadmap",
    "enterprise architecture team",
    "gcp compete",
    "goals: new to goals 101",
    "gong collaborator licenses",
    "google google cloud next 2026",
    "gpt compete",
    "weekly forecast",
    "zoom interview",
}

_TITLE_SUBSTRINGS = [
    "weekly enablement",
    "weekly team meeting",
    "1:1",
    "touchbase",
    "touch base",
    "breathe",
    "stretch",
    "hiring sync",
    "hiring interview",
    "hiring manager",
    "recruiting",
    "interlock",
    "internal collaboration",
    "v-team",
    "vteam",
    "office hours",
    "bi-weekly sync",
    "biweekly sync",
    "enablement",
    "irresistible",
    "stress management",
    "feedback skills",
    "industry principles",
]

_TITLE_REGEXES = [
    re.compile(r"^[a-z]+(?: [a-z]+)? [/&] [a-z]+(?: [a-z]+)? (?:sync|touchbase|touch base|weekly|bi.weekly|1:1)", re.I),
]


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.rstrip(":")
    s = re.sub(r"\s+", " ", s)
    return s


def is_internal_call(customer: str | None, title: str | None) -> bool:
    if customer:
        if _norm(customer) in _INTERNAL_CUSTOMERS_NORM:
            return True
    if title:
        t = (title or "").strip().lower()
        for sub in _TITLE_SUBSTRINGS:
            if sub in t:
                return True
        for pat in _TITLE_REGEXES:
            if pat.search(title.strip()):
                return True
    return False

