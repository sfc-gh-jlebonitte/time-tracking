from __future__ import annotations


def fetch_all_se_accounts(con, se_name: str) -> set[str]:
    """
    Return all distinct Salesforce account names this SE has ever appeared
    against in DIM_SE_ACTIVITY (not time-bounded).
    """
    sql = """
        SELECT DISTINCT ACCOUNT_NAME
        FROM SALES.SE_REPORTING.DIM_SE_ACTIVITY
        WHERE ACTIVITY_SE_NAME = %s
          AND ACCOUNT_NAME IS NOT NULL
          AND TRIM(ACCOUNT_NAME) != ''
    """
    try:
        with con.cursor() as cur:
            cur.execute(sql, (se_name,))
            return {str(row[0]).strip() for row in cur.fetchall() if row and row[0]}
    except Exception:
        return set()
