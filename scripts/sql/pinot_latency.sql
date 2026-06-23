-- GPT rollout Pinot latency metrics (TTFB by brand, split by conversation turn).
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
-- Each metric is computed twice using FILTER (WHERE ...) on the aggregate:
--
--   first_*    -> conversation_turn = 1  (initial utterance latency; what
--                                         users feel most when they speak)
--   followup_* -> conversation_turn != 1 (subsequent turns in the same
--                                         conversation; typically faster
--                                         because model state is warm)
--
-- The bridge then runs `assert_select_only(rendered_sql)` to confirm the
-- query is a single SELECT statement with no DDL / DML keywords before
-- POSTing it to Pinot. The Pinot endpoint itself only accepts one statement
-- per request, but the bridge guard is defense-in-depth.
SELECT
  brand_id,
  COUNT(*) FILTER (WHERE conversation_turn = 1) AS first_total_requests,
  AVG(duration) FILTER (WHERE conversation_turn = 1) AS first_avg_ttfb_ms,
  PERCENTILETDIGEST(duration, 50) FILTER (WHERE conversation_turn = 1) AS first_p50_ttfb_ms,
  PERCENTILETDIGEST(duration, 95) FILTER (WHERE conversation_turn = 1) AS first_p95_ttfb_ms,
  PERCENTILETDIGEST(duration, 99) FILTER (WHERE conversation_turn = 1) AS first_p99_ttfb_ms,
  COUNT(*) FILTER (WHERE conversation_turn != 1) AS followup_total_requests,
  AVG(duration) FILTER (WHERE conversation_turn != 1) AS followup_avg_ttfb_ms,
  PERCENTILETDIGEST(duration, 50) FILTER (WHERE conversation_turn != 1) AS followup_p50_ttfb_ms,
  PERCENTILETDIGEST(duration, 95) FILTER (WHERE conversation_turn != 1) AS followup_p95_ttfb_ms,
  PERCENTILETDIGEST(duration, 99) FILTER (WHERE conversation_turn != 1) AS followup_p99_ttfb_ms
FROM prod_service_metrics
WHERE service = 'ai_simulation'
  AND story_type = 'gpt'
  AND duration < 10000
  AND first_chunk_timestamp >= now() - __WINDOW_MS__
  AND brand_id IN (__BRAND_IDS__)
GROUP BY brand_id
ORDER BY first_p95_ttfb_ms DESC
