from __future__ import annotations

import re
from dataclasses import dataclass


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    s = s.strip().lower()
    s = s.replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _tokens(s: str) -> set[str]:
    n = _norm(s)
    if not n:
        return set()
    toks = set(n.split(" "))
    stop = {
        "the",
        "and",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "company",
        "co",
        "group",
        "meeting",
        "sync",
        "weekly",
        "working",
        "session",
        "poc",
        "snowflake",
        "bigquery",
        "vs",
        "prep",
        "pre",
        "call",
        "internal",
    }
    return {t for t in toks if t and t not in stop and len(t) > 1}


def _strip_suffixes(s: str) -> str:
    s = s.strip()
    # drop trailing parenthetical qualifiers
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    # drop explicit Snowflake pairing noise
    s = re.sub(r"(?i)\s*&\s*snowflake\s*$", "", s).strip()
    s = re.sub(r"(?i)\s*\|\s*snowflake\s*$", "", s).strip()
    s = re.sub(r"(?i)^\s*snowflake\s*&\s*", "", s).strip()
    # drop common trailing words that are not part of the account name
    s = re.sub(r"(?i)\s+\bmeeting\b\s*$", "", s).strip()
    s = re.sub(r"(?i)\s+\bsync\b\s*$", "", s).strip()
    s = re.sub(r"(?i)\s+\bprep\b\s*$", "", s).strip()
    # drop "Quote Q&A" descriptors
    s = re.sub(r"(?i)\s+quote\s+q\s*&?\s*a\s*$", "", s).strip()
    return s


def _best_match_against(preferred_accounts: set[str], candidate: str) -> str | None:
    cand_n = _norm(candidate)
    if not cand_n:
        return None

    best: tuple[float, str] | None = None
    cand_t = _tokens(candidate)

    for acc in preferred_accounts:
        acc_n = _norm(acc)
        if not acc_n:
            continue
        if acc_n in {"pre"}:
            continue
        # exact/substring wins
        if cand_n == acc_n:
            return acc
        if cand_n and (cand_n in acc_n or acc_n in cand_n):
            score = 0.95 + min(0.03, 0.001 * len(acc_n))
        else:
            acc_t = _tokens(acc)
            if not acc_t or not cand_t:
                continue
            inter = len(acc_t & cand_t)
            union = len(acc_t | cand_t)
            score = inter / union if union else 0.0
        if best is None or score > best[0] or (score == best[0] and len(acc) > len(best[1])):
            best = (score, acc)

    if best and best[0] >= 0.55:
        return best[1]
    return None


def canonicalize_customer(name: str, *, preferred_accounts: set[str] | None = None) -> str:
    """
    Collapse known naming variants to a canonical customer name.

    This is intended for reporting/aggregation and is deliberately conservative and explicit.
    """
    s = (name or "").strip()
    if not s:
        return s

    s = _strip_suffixes(s)
    n = _norm(s)
    if n in {"pre"}:
        return ""

    # Trans Union variants (e.g. "Trans Union LLC (Iovation)", "Trans Union LLC (Parent)")
    if n.startswith("trans union"):
        return "Trans Union LLC"

    # Verizon variants (e.g. "Verizon Corporate Systems Group", "Verizon VMS / FWA + Family")
    if n.startswith("verizon"):
        return "Verizon"

    # J.D. Power variants
    if n.startswith("jd power") or n.startswith("j d power"):
        if preferred_accounts:
            for acc in sorted(preferred_accounts, key=len, reverse=True):
                acc_n = _norm(acc)
                if acc_n.startswith("j d power") or acc_n.startswith("jd power"):
                    return acc
        return "J.D. Power and Associates"

    # CBRE variants
    if n.startswith("cbre"):
        return "CBRE"

    # If we have a set of "official" accounts from Snowhouse (structured fields),
    # snap short/variant names to the best matching official name.
    if preferred_accounts:
        m = _best_match_against(preferred_accounts, s)
        if m:
            return m

    return s


@dataclass(frozen=True)
class AttributionResult:
    customer: str | None
    confidence: float
    reason: str


class CustomerAttributor:
    """
    Post-process Snowhouse rows to attribute a 'customer' when the structured fields are missing.

    This is intentionally deterministic (fast + predictable). It uses:
    - simple title patterns (e.g. "Snowflake & X - ...", "... @ X", "X | Snowflake")
    - fuzzy matching against known accounts seen elsewhere in the same report window
    """

    def __init__(self, known_accounts: set[str], *, preferred_accounts: set[str] | None = None):
        self._preferred_accounts = {a.strip() for a in (preferred_accounts or set()) if a and a.strip()}
        self._known_accounts = {
            canonicalize_customer(a.strip(), preferred_accounts=self._preferred_accounts)
            for a in known_accounts
            if a and a.strip()
        }
        self._known_norm = {a: _norm(a) for a in self._known_accounts}
        self._known_tokens = {a: _tokens(a) for a in self._known_accounts}

    @staticmethod
    def build_known_accounts(rows: list[dict]) -> set[str]:
        out: set[str] = set()
        for r in rows:
            for k in ("CUSTOMER_ACCOUNT", "ACCOUNT_NAME"):
                v = r.get(k)
                if isinstance(v, str) and v.strip():
                    if _norm(v) in {"pre"}:
                        continue
                    out.add(v.strip())

            # Also learn from titles (pattern extraction only), so we can later
            # match internal prep calls like "[Internal] - Grafana Prep-Call".
            for k in ("MEETING_TITLE", "GONG_TITLE", "ACTIVITY_DESCRIPTION"):
                v = r.get(k)
                if not isinstance(v, str) or not v.strip():
                    continue
                extracted = CustomerAttributor._extract_from_title(v.strip())
                if extracted:
                    if _norm(extracted) in {"pre"}:
                        continue
                    out.add(extracted)
        return out

    @staticmethod
    def build_preferred_accounts(rows: list[dict]) -> set[str]:
        """
        Accounts from structured Snowhouse fields; treated as "official" names for snapping.
        """
        out: set[str] = set()
        for r in rows:
            for k in ("CUSTOMER_ACCOUNT", "ACCOUNT_NAME"):
                v = r.get(k)
                if isinstance(v, str) and v.strip():
                    cleaned = _strip_suffixes(v.strip())
                    if _norm(cleaned) in {"pre"}:
                        continue
                    out.add(cleaned)
        return out

    def attribute(self, *, title: str, context: str | None = None) -> AttributionResult:
        t = (title or "").strip()
        if not t:
            return AttributionResult(customer=None, confidence=0.0, reason="empty-title")

        # 1) High-precision pattern extraction.
        extracted = self._extract_from_title(t)
        if extracted:
            extracted = canonicalize_customer(extracted, preferred_accounts=self._preferred_accounts)
            # if we extracted something that matches a known account strongly, snap to it
            snapped = self._snap_to_known(extracted)
            if snapped:
                return AttributionResult(
                    customer=canonicalize_customer(snapped, preferred_accounts=self._preferred_accounts),
                    confidence=0.92,
                    reason=f"pattern+snap:{extracted}",
                )
            return AttributionResult(customer=extracted, confidence=0.82, reason="pattern-extract")

        # 2) Fuzzy match against known accounts (title).
        best = self._fuzzy_match_known(t)
        if best:
            cust, score = best
            return AttributionResult(
                customer=canonicalize_customer(cust, preferred_accounts=self._preferred_accounts),
                confidence=score,
                reason="fuzzy-known",
            )

        # 3) Use meeting context (summary/next steps/etc) when available.
        ctx = (context or "").strip()
        if ctx:
            # Try to find a direct account mention first.
            direct = self._find_known_substring(ctx)
            if direct:
                return AttributionResult(
                    customer=canonicalize_customer(direct, preferred_accounts=self._preferred_accounts),
                    confidence=0.84,
                    reason="context-substring",
                )

            best2 = self._fuzzy_match_known(ctx)
            if best2:
                cust, score = best2
                return AttributionResult(
                    customer=canonicalize_customer(cust, preferred_accounts=self._preferred_accounts),
                    confidence=min(0.80, score),
                    reason="context-fuzzy",
                )

            extracted_ctx = self._extract_from_context(ctx)
            if extracted_ctx:
                extracted_ctx = canonicalize_customer(extracted_ctx, preferred_accounts=self._preferred_accounts)
                return AttributionResult(customer=extracted_ctx, confidence=0.78, reason="context-extract")

        return AttributionResult(customer=None, confidence=0.0, reason="no-match")

    @staticmethod
    def _extract_from_title(title: str) -> str | None:
        s = title.strip()

        # Strip internal/prefix noise so patterns can see the customer.
        s = re.sub(r"(?i)^\s*\(\s*internal\s*\)\s*", "", s).strip()
        s = re.sub(r"(?i)^\s*\[\s*internal\s*\]\s*", "", s).strip()
        s = re.sub(r"(?i)^\s*int\s*:\s*", "", s).strip()
        s = re.sub(r"(?i)^\s*internal\s*:\s*", "", s).strip()
        s = re.sub(r"(?i)^\s*snowflake\s+internal\s*-\s*", "", s).strip()

        patterns: list[re.Pattern[str]] = [
            # "Autofleet/Snowflake: ...", "Cargo/Snowflake ..."
            re.compile(r"(?i)^([^/|@-]{2,}?)\s*/\s*Snowflake\b"),
            # "Humana GCP: ...", "Humana GCP - ..."
            re.compile(r"(?i)^(.+?)\s+GCP\b"),
            # "... google compete"
            re.compile(r"(?i)^(.+?)\s+google\s+compete\b"),
            # "Strategic Support for Banco Mercantil (...)" / "Support for X ("
            re.compile(r"(?i)\b(?:strategic\s+support|support)\s+for\s+(.+?)(?:\s*\(|\s*$)"),
            # "Pre-call for upcoming RGA meeting ..."
            re.compile(r"(?i)\bpre[- ]call\b.*\bupcoming\s+([A-Z]{2,12})\b.*\bmeeting\b"),
            # "talk ID.Me"
            re.compile(r"(?i)\btalk\s+([A-Za-z0-9.]+)\b"),
            # "Snowflake Internal - McKesson ... - ..." (after stripping prefix, capture up to next " - ")
            re.compile(r"(?i)^([^|@-]{3,}?)\s*-\s*"),
            re.compile(r"(?i)\bSnowflake\s*&\s*(.+?)\s*-\s*"),
            re.compile(r"(?i)\bSnowflake\s+and\s+(.+?)\s*-\s*"),
            re.compile(r"(?i)\bSnowflake\s+vs\.\s+BigQuery\s+@\s+(.+)$"),
            re.compile(r"(?i)@\s*([^|]+)$"),
            re.compile(r"(?i)^([^|]+?)\s*\|\s*Snowflake\b"),
            re.compile(r"(?i)\bSnowflake\s*\|\s*([^|]+)$"),
            # Prep patterns: "Moloco Prep for ...", "First Financial quick prep"
            re.compile(r"(?i)^(.+?)\s+(?:quick\s+)?prep\b"),
            # Generic "Customer - ..." patterns (after internal prefix stripping)
            re.compile(r"(?i)^([^-|@]{3,}?)\s*-\s*"),
        ]
        for p in patterns:
            m = p.search(s)
            if not m:
                continue
            cand = m.group(1).strip()
            cand = re.sub(r"\s+#\d+\s*$", "", cand).strip()
            cand = re.sub(r"\s*\(.*?\)\s*$", "", cand).strip()
            cand = cand.strip(" -–—|")
            # drop obvious non-customers
            if not cand or len(cand) < 3:
                continue
            if any(x in cand.lower() for x in ("internal", "1:1", "personal meeting room")):
                continue
            if cand.strip().lower() == "snowflake":
                continue
            return cand

        # Special case: "Snowflake & CenterPoint Energy - POC Working Session #1"
        m = re.search(r"(?i)\bSnowflake\s*&\s*(.+?)\s*-\s*POC\b", s)
        if m:
            cand = m.group(1).strip().strip(" -–—|")
            return cand or None

        return None

    @staticmethod
    def _extract_from_context(context: str) -> str | None:
        """
        Heuristic extraction when the title is generic (e.g. "Zoom Meeting").
        """
        ctx = context.strip()
        if not ctx:
            return None

        # Common phrasing in summaries: "X from Cargo", "along with Aurelien from Cargo"
        for pat in [
            re.compile(r"\bfrom\s+([A-Z][A-Za-z0-9.&/-]+(?:\s+[A-Z][A-Za-z0-9.&/-]+){0,3})"),
            re.compile(r"\bwith\s+([A-Z][A-Za-z0-9.&/-]+(?:\s+[A-Z][A-Za-z0-9.&/-]+){0,3})"),
            re.compile(r"\bdiscussed\s+([A-Z][A-Za-z0-9.&/-]+(?:\s+[A-Z][A-Za-z0-9.&/-]+){0,3})"),
        ]:
            m = pat.search(ctx)
            if not m:
                continue
            cand = m.group(1).strip().strip(" .,:;()[]{}\"'")
            if not cand or len(cand) < 3:
                continue
            low = cand.lower()
            if "snowflake" in low:
                continue
            return cand

        # Possessive: "Cargo's high Snowflake costs..."
        m = re.search(r"\b([A-Z][A-Za-z0-9.&/-]{2,})'s\b", ctx)
        if m:
            cand = m.group(1).strip()
            if cand.lower() != "snowflake":
                return cand

        return None

    def _find_known_substring(self, text: str) -> str | None:
        t_norm = _norm(text)
        if not t_norm:
            return None
        # Prefer longest matches to avoid "Verizon" beating "Verizon VMS ..." in raw text;
        # canonicalization later will collapse anyway.
        for acc, acc_n in sorted(self._known_norm.items(), key=lambda kv: len(kv[1]), reverse=True):
            if not acc_n:
                continue
            # For single-token accounts, require whole-word match (avoid "pre" matching "prep").
            if " " not in acc_n:
                if acc_n in set(t_norm.split(" ")):
                    return acc
                continue
            if acc_n in t_norm:
                return acc
        return None

    def _snap_to_known(self, extracted: str) -> str | None:
        ex_n = _norm(extracted)
        if not ex_n:
            return None
        for acc, acc_n in self._known_norm.items():
            if ex_n == acc_n:
                return acc
        # substring snap
        for acc, acc_n in self._known_norm.items():
            if ex_n in acc_n or acc_n in ex_n:
                return acc
        return None

    def _fuzzy_match_known(self, title: str) -> tuple[str, float] | None:
        t_norm = _norm(title)
        t_toks = _tokens(title)
        if not t_norm:
            return None

        t_words = set(t_norm.split(" "))
        best_acc = None
        best = 0.0
        for acc, acc_n in self._known_norm.items():
            # easy win
            if acc_n:
                if " " not in acc_n:
                    if acc_n in t_words:
                        return acc, 0.86
                else:
                    if acc_n in t_norm:
                        return acc, 0.86
            # token overlap
            acc_toks = self._known_tokens.get(acc) or set()
            if not acc_toks or not t_toks:
                continue
            inter = len(acc_toks & t_toks)
            union = len(acc_toks | t_toks)
            score = inter / union if union else 0.0
            if score > best:
                best = score
                best_acc = acc

        if best_acc and best >= 0.45:
            # scale into a more human-ish confidence band
            return best_acc, min(0.78, 0.55 + best * 0.35)
        return None

