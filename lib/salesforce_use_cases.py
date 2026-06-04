from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class OpportunityUseCaseRow:
    opp_id: str | None
    opp_name: str | None
    account_name: str | None
    amount: float | None
    use_case: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class UseCaseDiscovery:
    database: str
    schema: str
    table: str
    opp_id_col: str | None
    opp_name_col: str | None
    account_name_col: str | None
    amount_col: str | None
    use_case_col: str
    score: int


def _fetchall_dict(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchall()
    return [{cols[i]: r[i] for i in range(len(cols))} for r in rows]


def _uniq_strs(xs: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in xs:
        s = (x or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _split_use_cases(raw: str) -> list[str]:
    """
    Salesforce multi-select picklists are often semi-colon delimited; some pipelines
    may use commas. We treat both as separators and keep deterministic ordering.
    """
    s = (raw or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.replace(",", ";").split(";")]
    return _uniq_strs(parts)


def _score_candidate(
    *,
    table_name: str,
    cols: set[str],
    use_case_col: str,
    opp_id_col: str | None,
    opp_name_col: str | None,
    amount_col: str | None,
    account_name_col: str | None,
) -> int:
    tn = table_name.upper()
    score = 0
    if "OPPORTUNITY" in tn or tn.startswith("OPP"):
        score += 30
    if "SFDC" in tn or "SALESFORCE" in tn:
        score += 15
    if "USE_CASE" in tn or "USECASE" in tn:
        score += 10

    score += 60 if opp_id_col else 0
    score += 25 if opp_name_col else 0
    score += 30 if amount_col else 0
    score += 10 if account_name_col else 0

    # Favor "custom field" patterns commonly used in SFDC extracts.
    if use_case_col.upper().endswith("__C"):
        score += 10

    # Mild penalty if the table looks like a log/event table.
    if any(x in tn for x in ("HISTORY", "EVENT", "AUDIT", "CHANGE")):
        score -= 10

    # Favor tables with a sane number of columns (dims over raw extracts).
    if len(cols) > 300:
        score -= 10
    return score


def discover_salesforce_use_case_source(
    *,
    con,
    databases: list[str],
    limit_tables: int = 200,
) -> tuple[UseCaseDiscovery | None, dict[str, Any]]:
    """
    Best-effort discovery for a table that contains:
    - an opportunity id or name
    - a use-case field
    - optionally amount + account name

    Returns the best-scoring candidate plus diagnostics.
    """
    diag: dict[str, Any] = {"searched_databases": list(databases), "candidates": [], "errors": []}
    candidates: list[UseCaseDiscovery] = []

    # Column preference lists (uppercased comparisons).
    opp_id_prefs = ["OPP_ID", "OPPORTUNITY_ID", "OPPORTUNITYID", "ID"]
    opp_name_prefs = ["OPP_NAME", "OPPORTUNITY_NAME", "OPPORTUNITYNAME", "NAME"]
    account_name_prefs = ["ACCOUNT_NAME", "ACCOUNTNAME", "ACCOUNT"]
    amount_prefs = ["AMOUNT", "OPP_AMOUNT", "OPPORTUNITY_AMOUNT", "TCV", "ACV", "ARR"]

    for db in databases:
        db = (db or "").strip()
        if not db:
            continue
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    select table_schema, table_name, column_name
                    from {db}.information_schema.columns
                    where column_name ilike %s
                      and (
                        table_name ilike %s
                        or table_name ilike %s
                        or table_name ilike %s
                      )
                    order by table_schema, table_name, column_name
                    limit %s
                    """,
                    ("%USE_CASE%", "%OPPORT%", "%OPP%", "%USE_CASE%", limit_tables),
                )
                hits = _fetchall_dict(cur)
        except Exception as e:  # noqa: BLE001
            diag["errors"].append({"database": db, "step": "find_use_case_columns", "error": str(e)})
            continue

        # Group by table.
        by_table: dict[tuple[str, str], set[str]] = {}
        for h in hits:
            schema = str(h.get("TABLE_SCHEMA") or "").strip()
            table = str(h.get("TABLE_NAME") or "").strip()
            col = str(h.get("COLUMN_NAME") or "").strip().upper()
            if not schema or not table or not col:
                continue
            by_table.setdefault((schema, table), set()).add(col)

        for (schema, table), use_case_cols in by_table.items():
            # Fetch all columns for scoring + selecting.
            try:
                with con.cursor() as cur:
                    cur.execute(
                        f"""
                        select column_name
                        from {db}.information_schema.columns
                        where table_schema = %s and table_name = %s
                        """,
                        (schema, table),
                    )
                    cols = {str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0]}
            except Exception as e:  # noqa: BLE001
                diag["errors"].append({"database": db, "step": "fetch_table_columns", "table": f"{schema}.{table}", "error": str(e)})
                continue

            def pick(prefs: list[str]) -> str | None:
                for p in prefs:
                    if p in cols:
                        return p
                return None

            opp_id_col = pick(opp_id_prefs)
            opp_name_col = pick(opp_name_prefs)
            account_name_col = pick(account_name_prefs)
            amount_col = pick(amount_prefs)

            # Pick one use-case column deterministically.
            use_case_col = sorted(use_case_cols)[0]

            score = _score_candidate(
                table_name=table,
                cols=cols,
                use_case_col=use_case_col,
                opp_id_col=opp_id_col,
                opp_name_col=opp_name_col,
                amount_col=amount_col,
                account_name_col=account_name_col,
            )
            cand = UseCaseDiscovery(
                database=db,
                schema=schema,
                table=table,
                opp_id_col=opp_id_col,
                opp_name_col=opp_name_col,
                account_name_col=account_name_col,
                amount_col=amount_col,
                use_case_col=use_case_col,
                score=score,
            )
            candidates.append(cand)
            diag["candidates"].append(
                {
                    "database": db,
                    "schema": schema,
                    "table": table,
                    "use_case_col": use_case_col,
                    "opp_id_col": opp_id_col,
                    "opp_name_col": opp_name_col,
                    "account_name_col": account_name_col,
                    "amount_col": amount_col,
                    "score": score,
                }
            )

    if not candidates:
        return None, diag
    best = sorted(candidates, key=lambda c: (-c.score, c.database, c.schema, c.table, c.use_case_col))[0]
    diag["selected"] = {
        "database": best.database,
        "schema": best.schema,
        "table": best.table,
        "use_case_col": best.use_case_col,
        "opp_id_col": best.opp_id_col,
        "opp_name_col": best.opp_name_col,
        "account_name_col": best.account_name_col,
        "amount_col": best.amount_col,
        "score": best.score,
    }
    return best, diag


def fetch_salesforce_use_cases_for_opps(
    *,
    con,
    opp_ids: set[str],
    opp_names: set[str],
    databases: list[str],
    limit_rows: int = 20000,
) -> tuple[list[OpportunityUseCaseRow], dict[str, Any]]:
    """
    Fetch use cases + opportunity value for a set of opportunities.

    This is best-effort: it discovers a likely source table, then queries it using
    whatever keys are available (opp_id preferred, otherwise opp_name).
    """
    source, diag = discover_salesforce_use_case_source(con=con, databases=databases)
    if source is None:
        diag["status"] = "no_source_found"
        return [], diag

    opp_ids_clean = _uniq_strs(sorted(opp_ids))
    opp_names_clean = _uniq_strs(sorted(opp_names))

    where_bits: list[str] = []
    params: list[Any] = []
    if source.opp_id_col and opp_ids_clean:
        where_bits.append(f"{source.opp_id_col} in ({','.join(['%s'] * len(opp_ids_clean))})")
        params.extend(opp_ids_clean)
    if source.opp_name_col and opp_names_clean:
        where_bits.append(f"{source.opp_name_col} in ({','.join(['%s'] * len(opp_names_clean))})")
        params.extend(opp_names_clean)

    if not where_bits:
        diag["status"] = "no_filter_keys"
        return [], diag

    select_cols: list[tuple[str, str]] = []
    if source.opp_id_col:
        select_cols.append((source.opp_id_col, "OPP_ID"))
    if source.opp_name_col:
        select_cols.append((source.opp_name_col, "OPP_NAME"))
    if source.account_name_col:
        select_cols.append((source.account_name_col, "ACCOUNT_NAME"))
    if source.amount_col:
        select_cols.append((source.amount_col, "AMOUNT"))
    select_cols.append((source.use_case_col, "USE_CASE_RAW"))

    select_sql = ",\n      ".join(f"{col} as {alias}" for col, alias in select_cols)
    sql = f"""
    select
      {select_sql}
    from {source.database}.{source.schema}.{source.table}
    where {" or ".join(where_bits)}
    limit %s
    """
    params.append(limit_rows)

    rows: list[OpportunityUseCaseRow] = []
    try:
        with con.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in (cur.description or [])]
            for tup in cur.fetchall():
                d = {cols[i]: tup[i] for i in range(len(cols))}
                use_raw = (d.get("USE_CASE_RAW") or "").strip() if isinstance(d.get("USE_CASE_RAW"), str) else str(d.get("USE_CASE_RAW") or "").strip()
                for uc in _split_use_cases(use_raw):
                    rows.append(
                        OpportunityUseCaseRow(
                            opp_id=(str(d.get("OPP_ID")).strip() if d.get("OPP_ID") is not None else None) or None,
                            opp_name=(str(d.get("OPP_NAME")).strip() if d.get("OPP_NAME") is not None else None) or None,
                            account_name=(str(d.get("ACCOUNT_NAME")).strip() if d.get("ACCOUNT_NAME") is not None else None) or None,
                            amount=(float(d.get("AMOUNT")) if d.get("AMOUNT") is not None and str(d.get("AMOUNT")).strip() else None),
                            use_case=uc,
                            raw=d,
                        )
                    )
    except Exception as e:  # noqa: BLE001
        diag["status"] = "query_failed"
        diag["error"] = str(e)
        diag["query"] = {"sql": sql, "params_summary": {"opp_ids": len(opp_ids_clean), "opp_names": len(opp_names_clean)}}
        return [], diag

    diag["status"] = "ok"
    diag["rows"] = len(rows)
    return rows, diag

