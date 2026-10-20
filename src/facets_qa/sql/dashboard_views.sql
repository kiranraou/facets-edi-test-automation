-- Dashboard views over the Delta results tables (Power BI / Lakeview / Streamlit).
-- __SCHEMA__ is replaced with <catalog>.<schema> at runtime.

-- One row per run: headline KPIs.
CREATE OR REPLACE VIEW __SCHEMA__.vw_elig_run_summary AS
SELECT
    run_id,
    MIN(run_ts)                                              AS run_ts,
    MIN(service_date)                                        AS service_date,
    COUNT(*)                                                 AS total_checks,
    SUM(CASE WHEN result = 'PASS'  THEN 1 ELSE 0 END)        AS passed,
    SUM(CASE WHEN result = 'FAIL'  THEN 1 ELSE 0 END)        AS failed,
    SUM(CASE WHEN result = 'ERROR' THEN 1 ELSE 0 END)        AS errored,
    ROUND(100.0 * SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS pass_rate_pct,
    SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END)            AS retried_checks,
    PERCENTILE_APPROX(latency_ms, 0.95)                      AS p95_latency_ms
FROM __SCHEMA__.elig_test_results
GROUP BY run_id;

-- Pass rate by scenario per run (active / terminated / future / indicator-off / not-found / invalid ID).
CREATE OR REPLACE VIEW __SCHEMA__.vw_elig_scenario_results AS
SELECT
    run_id, scenario,
    COUNT(*)                                                 AS total_checks,
    SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END)         AS passed,
    ROUND(100.0 * SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS pass_rate_pct
FROM __SCHEMA__.elig_test_results
GROUP BY run_id, scenario;

-- Failure trend: failures by reason category per run date for the last 90 days.
CREATE OR REPLACE VIEW __SCHEMA__.vw_elig_failure_trend AS
SELECT
    CAST(run_ts AS DATE)                                     AS run_date,
    CASE
        WHEN result = 'ERROR' AND failure_reason LIKE 'gateway:%'       THEN 'Gateway unavailable'
        WHEN result = 'ERROR' AND failure_reason LIKE '271 parse:%'     THEN '271 parse / contract'
        WHEN result = 'ERROR' AND failure_reason LIKE 'data contract:%' THEN 'Facets data quality'
        WHEN failure_reason LIKE '%AAA reason%'                          THEN 'Wrong AAA reject reason'
        WHEN failure_reason LIKE '%coverage%'                            THEN 'Coverage date mismatch'
        WHEN failure_reason LIKE '%but 271 says%'                        THEN 'Eligibility status mismatch'
        ELSE 'Other'
    END                                                      AS failure_category,
    COUNT(*)                                                 AS failures
FROM __SCHEMA__.elig_test_results
WHERE result <> 'PASS'
  AND run_ts >= DATE_SUB(CURRENT_DATE(), 90)
GROUP BY 1, 2;
