-- GPT rollout Pinot latency metrics (TTFB by brand).
--
-- Consumed by scripts/local_data_bridge.py via the /query/pinot/latency
-- endpoint. The bridge substitutes two placeholders before sending the query
-- to the StarTree Pinot /sql endpoint:
--
--   __WINDOW_MS__   replaced with a validated positive integer (number of
--                   milliseconds in the rolling window). days * 86400000.
--   __BRAND_IDS__   replaced with a comma-separated list of validated
--                   positive integers (brand ids requested).
--
-- The bridge then runs `_assert_select_only(rendered_sql)` to confirm the
-- query is a single SELECT statement with no DDL / DML keywords before
-- POSTing it to Pinot. The Pinot endpoint itself only accepts one statement
-- per request, but the bridge guard is defense-in-depth.
SELECT
  brand_id,
  COUNT(*) AS total_requests,
  AVG(duration) AS avg_ttfb_ms,
  PERCENTILETDIGEST(duration, 50) AS p50_ttfb_ms,
  PERCENTILETDIGEST(duration, 95) AS p95_ttfb_ms,
  PERCENTILETDIGEST(duration, 99) AS p99_ttfb_ms
FROM prod_service_metrics
WHERE service = 'ai_simulation'
  AND story_type = 'gpt'
  AND duration < 10000
  AND first_chunk_timestamp >= now() - __WINDOW_MS__
  AND brand_id IN (__BRAND_IDS__)
GROUP BY brand_id
ORDER BY p95_ttfb_ms DESC
