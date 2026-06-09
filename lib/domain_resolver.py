from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_NOISE = re.compile(
    r"(?i)^(home|welcome\s+to|official\s+site|official\s+website|homepage|"
    r"error|page\s+not\s+found|access\s+denied|403|404)\s*$"
)
_TITLE_SPLIT = re.compile(r"\s*[|\-–—]\s*")


def _extract_company_from_html(html: str) -> str | None:
    # 1) og:site_name
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\'](.*?)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:site_name["\']', html, re.I)
    if m:
        val = m.group(1).strip()
        if val and not _NOISE.match(val):
            return val

    # 2) application-name
    m = re.search(r'<meta[^>]+name=["\']application-name["\'][^>]+content=["\'](.*?)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']application-name["\']', html, re.I)
    if m:
        val = m.group(1).strip()
        if val and not _NOISE.match(val):
            return val

    # 3) <title> — take the longest meaningful segment
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        parts = [p.strip() for p in _TITLE_SPLIT.split(raw) if p.strip()]
        parts = [p for p in parts if not _NOISE.match(p) and len(p) > 2]
        if parts:
            return max(parts, key=len)

    return None


class DomainResolver:
    """
    Resolves an email domain to a company name by fetching the domain homepage.
    Results are cached to avoid re-fetching on every report run.
    """

    def __init__(self, cache_path: Path):
        self._cache_path = cache_path
        self._cache: dict[str, Any] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def resolve(self, domain: str) -> str | None:
        """Return company name for domain, or None if unresolvable."""
        key = domain.lower()
        if key in self._cache:
            return self._cache[key]  # may be None (cached miss)
        result = self._fetch(key)
        self._cache[key] = result
        return result

    def save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _fetch(self, domain: str) -> str | None:
        try:
            import urllib.request
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            for url in (f"https://www.{domain}", f"https://{domain}"):
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; weekly-report/1.0)"},
                    )
                    with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                        raw = resp.read(65536)
                    html = raw.decode("utf-8", errors="replace")
                    company = _extract_company_from_html(html)
                    if company:
                        return company
                except Exception:
                    continue
        except Exception:
            pass
        return None
