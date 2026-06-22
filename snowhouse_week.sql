-- Weekly Snowhouse query: Zoom + Gong + SE Activity for a given date range.
-- Accepts 9 bind parameters in this order:
--   zoom_meetings : user_email, week_start, week_end
--   gong_calls    : se_display_name, week_start, week_end
--   se_activities : se_display_name, week_start, week_end

WITH zoom_meetings AS (
    SELECT DISTINCT
        m.ID                  AS MEETING_ID,
        m.TOPIC               AS MEETING_TITLE,
        m.HOST                AS MEETING_HOST,
        m.START_TIME          AS MEETING_DATETIME
    FROM IT.RAW_ZOOM_CUSTOM.ZOOM_MEETINGS m
    JOIN IT.RAW_ZOOM_CUSTOM.ZOOM_MEETING_PARTICIPANTS p
        ON m.ID = p.MEETING_ID
    WHERE p.EMAIL = %s
      AND m.START_TIME::DATE BETWEEN %s AND %s
),
gong_calls AS (
    SELECT DISTINCT
        c.CONVERSATION_KEY,
        c.TITLE                       AS GONG_TITLE,
        c.EFFECTIVE_START_DATETIME    AS GONG_DATETIME,
        c.CALL_SPOTLIGHT_BRIEF        AS GONG_SUMMARY,
        c.CALL_SPOTLIGHT_NEXT_STEPS   AS GONG_NEXT_STEPS,
        c.CALL_SPOTLIGHT_KEY_POINTS   AS GONG_KEY_POINTS,
        c.CALL_URL                    AS GONG_URL
    FROM GONG_SHARE.GONG_DATA_CLOUD.CALLS c
    JOIN GONG_SHARE.GONG_DATA_CLOUD.CONVERSATION_PARTICIPANTS cp
        ON c.CONVERSATION_KEY = cp.CONVERSATION_KEY
    WHERE cp.NAME = %s
      AND c.EFFECTIVE_START_DATETIME::DATE BETWEEN %s AND %s
),
se_activities AS (
    SELECT
        ACTIVITY_ID,
        ACTIVITY_DATE,
        ACTIVITY_DESCRIPTION,
        ACCOUNT_NAME,
        ACCOUNT_ID,
        OPP_NAME,
        OPP_ID,
        USE_CASE_ID,
        IS_EXTERNAL,
        MEETING_STATUS,
        ACTIVITY_SE_NAME,
        ACTIVITY_SE_HIERARCHY_EMAIL
    FROM SALES.SE_REPORTING.DIM_SE_ACTIVITY
    WHERE ACTIVITY_SE_NAME = %s
      AND ACTIVITY_TYPE    = 'Meeting'
      AND ACTIVITY_DATE    BETWEEN %s AND %s
),
gong_unmatched AS (
    SELECT g.GONG_TITLE, g.GONG_DATETIME
    FROM gong_calls g
    LEFT JOIN zoom_meetings zm
        ON zm.MEETING_DATETIME::DATE = g.GONG_DATETIME::DATE
        AND ABS(DATEDIFF('minute', zm.MEETING_DATETIME, g.GONG_DATETIME)) <= 20
    WHERE zm.MEETING_ID IS NULL
),
se_unmatched AS (
    SELECT a.ACTIVITY_DESCRIPTION, a.ACTIVITY_DATE
    FROM se_activities a
    LEFT JOIN zoom_meetings zm
        ON zm.MEETING_DATETIME::DATE = a.ACTIVITY_DATE
        AND CONTAINS(a.ACTIVITY_DESCRIPTION, SPLIT_PART(zm.MEETING_TITLE, ' - ', 1))
    LEFT JOIN gong_calls g
        ON g.GONG_DATETIME::DATE = a.ACTIVITY_DATE
    WHERE a.MEETING_STATUS = 'MEETING_STATUS_COMPLETED'
      AND zm.MEETING_ID IS NULL
      AND g.CONVERSATION_KEY IS NULL
),
all_meetings AS (
    SELECT MEETING_ID, MEETING_TITLE, MEETING_HOST, MEETING_DATETIME, 'Zoom' AS PRIMARY_SOURCE
    FROM zoom_meetings

    UNION ALL

    SELECT NULL, GONG_TITLE, NULL, GONG_DATETIME::TIMESTAMP_NTZ, 'Gong'
    FROM gong_unmatched

    UNION ALL

    SELECT NULL, ACTIVITY_DESCRIPTION, NULL, ACTIVITY_DATE::TIMESTAMP_NTZ, 'SE Activity'
    FROM se_unmatched
)
SELECT
    am.MEETING_ID,
    am.MEETING_TITLE,
    am.MEETING_HOST,
    am.MEETING_DATETIME,
    am.PRIMARY_SOURCE,
    a.ACCOUNT_NAME    AS CUSTOMER_ACCOUNT,
    a.ACCOUNT_ID      AS SF_ACCOUNT_ID,
    a.ACTIVITY_ID     AS SF_ACTIVITY_ID,
    a.OPP_NAME,
    a.OPP_ID          AS SF_OPP_ID,
    a.USE_CASE_ID     AS SF_USE_CASE_ID,
    a.IS_EXTERNAL,
    a.ACTIVITY_SE_NAME              AS SE_NAME,
    a.ACTIVITY_SE_HIERARCHY_EMAIL   AS SE_HIERARCHY_EMAIL,
    g.GONG_SUMMARY                  AS SUMMARY,
    g.GONG_NEXT_STEPS               AS NEXT_STEPS,
    g.GONG_KEY_POINTS               AS KEY_POINTS,
    CASE WHEN g.GONG_SUMMARY IS NOT NULL THEN 'Gong' ELSE NULL END AS SUMMARY_SOURCE,
    g.GONG_URL                      AS CALL_URL,
    NULL                            AS TRANSCRIPT_URL,
    NULL                            AS RECORDING_PASSWORD
FROM all_meetings am
LEFT JOIN se_activities a
    ON am.MEETING_DATETIME::DATE = a.ACTIVITY_DATE
    AND CONTAINS(a.ACTIVITY_DESCRIPTION, SPLIT_PART(am.MEETING_TITLE, ' - ', 1))
LEFT JOIN gong_calls g
    ON am.MEETING_DATETIME::DATE = g.GONG_DATETIME::DATE
    AND ABS(DATEDIFF('minute', am.MEETING_DATETIME, g.GONG_DATETIME)) <= 20
ORDER BY am.MEETING_DATETIME
