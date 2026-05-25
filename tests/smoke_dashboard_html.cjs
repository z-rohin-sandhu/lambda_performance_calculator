// Smoke test: re-uses the JS logic from dashboards/gpt_rollout_dashboard.html
// to verify the in-browser pipeline matches the Python output on the samples.
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "dashboards", "gpt_rollout_dashboard.html"),
  "utf8"
);

const startTag = "<script>";
const endTag = "</script>";
const startIdx = html.indexOf(startTag) + startTag.length;
const endIdx = html.lastIndexOf(endTag);
const scriptSrc = html.slice(startIdx, endIdx);

// Strip the DOMContentLoaded bootstrap (no DOM here) and expose internals.
const cleanedSrc = scriptSrc
  .replace(/document\.addEventListener\("DOMContentLoaded"[\s\S]*\)\;\s*$/, "")
  .replace(/window\.addEventListener\("drag[\s\S]*?\)\;\s*$/m, "");

// Provide enough of a "window" + "document" surface for the bridge helpers to
// load without throwing under Node. The helpers in scope here mostly defend
// against null DOM lookups, so we just need the lookups to succeed and return
// a benign object. Anything inside the bridge code that touches the real DOM
// is exercised in the browser, not here.
const noopFn = () => null;
const stubDocument = {
  getElementById: noopFn,
  querySelectorAll: () => [],
  querySelector: noopFn,
  addEventListener: noopFn,
};
const stubLocalStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
  removeItem(key) { this.store.delete(key); },
};
const stubWindow = {
  localStorage: stubLocalStorage,
  setInterval: () => 0,
  clearInterval: noopFn,
  addEventListener: noopFn,
};
// Install stubs on globalThis so bare `window`/`document` references in the
// stripped HTML script resolve when the temp file is `require()`-d below.
globalThis.window = stubWindow;
globalThis.document = stubDocument;

// fetch is monkey-patched per-test below.
globalThis.fetch = async () => { throw new Error("fetch was not stubbed for this test"); };

const exportSrc = `
${cleanedSrc}
module.exports = {
  parseCsv, validateColumns, mergeOnBrandId, buildSummary, computeInsights,
  parseTopMessageList, normalizeLambdaRows, computeLambdaInsights,
  lambdaErrorTier, collectTopMessagesAcrossLambdas,
  LAMBDA_REQUIRED, MYSQL_REQUIRED, PINOT_REQUIRED,
  pingBridge, fetchMysqlAdoption, normalizeFetchedMysqlRows,
  parseBrandIdsInput, bridgeFetchUrl,
  fetchLambdaCloudwatch, normalizeFetchedLambdaRows, parseLambdaNamesInput,
  fetchPinotLatency, normalizeFetchedPinotRows,
  bucketHealth, worstHealth, classifyHealth,
  partitionBySample, computeCohortComparison, aggregateCohort,
  buildPinotOnlySummary,
};
`;

const tmpFile = path.join(__dirname, ".__dashboard_logic.cjs");
fs.writeFileSync(tmpFile, exportSrc);
let logic;
try {
  logic = require(tmpFile);
} finally {
  fs.unlinkSync(tmpFile);
}

const pinotPath = path.join(__dirname, "..", "examples", "sample_pinot_metrics.csv");
const mysqlPath = path.join(__dirname, "..", "examples", "sample_mysql_metrics.csv");

const pinotRows = logic.parseCsv(fs.readFileSync(pinotPath, "utf8"));
const mysqlRows = logic.parseCsv(fs.readFileSync(mysqlPath, "utf8"));
const merged = logic.mergeOnBrandId(pinotRows, mysqlRows);
const summary = logic.buildSummary(merged, {
  p95GreenMax: 5000, p95RedMin: 8000, p99GreenMax: 8000, p99RedMin: 15000,
});
const insights = logic.computeInsights(summary);

const expectedOrder = [
  "Globex Insurance", "Soylent Support", "Northwind Coaching",
  "Acme Health", "Initech Sales",
];
const actualOrder = summary.map((r) => r.brand_name);

const expectedStatuses = ["RED", "YELLOW", "YELLOW", "GREEN", "GREEN"];
const actualStatuses = summary.map((r) => r.health_status);

function assertEqual(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    console.error(`FAIL: ${label}\n  expected: ${e}\n  actual:   ${a}`);
    process.exit(1);
  }
  console.log(`ok: ${label}`);
}

assertEqual("sort order", actualOrder, expectedOrder);
assertEqual("statuses", actualStatuses, expectedStatuses);
assertEqual("slowest brand", insights.slowest.brand_name, "Globex Insurance");
assertEqual("most adopted", insights.mostAdopted.brand_name, "Initech Sales");
assertEqual("healthiest brand", insights.healthiest.brand_name, "Initech Sales");
assertEqual("counts", [insights.greenCount, insights.yellowCount, insights.redCount], [2, 2, 1]);

/* ------------------------------------------------------------------ */
/* Lambda CSV parity: parse the bundled sample and confirm sort order */
/* and lambda-insight picks match the source markdown report.         */
/* ------------------------------------------------------------------ */

const lambdaPath = path.join(__dirname, "..", "examples", "sample_lambda_metrics.csv");
const lambdaRows = logic.parseCsv(fs.readFileSync(lambdaPath, "utf8"));
logic.validateColumns(lambdaRows, logic.LAMBDA_REQUIRED, "Lambda CSV");
const lambdaSummary = logic.normalizeLambdaRows(lambdaRows);
const lambdaInsights = logic.computeLambdaInsights(lambdaSummary);
const topMessages = logic.collectTopMessagesAcrossLambdas(lambdaSummary);

assertEqual(
  "lambda sort order",
  lambdaSummary.map((row) => row.lambda_name),
  [
    "zen-prod-sqs-message-consumer",
    "zen-prod-ws-to-sqs-producer",
    "zen_prod_authorizer_service",
  ],
);
// Service-level metrics are clean: lambda_errors / throttles are all zero, but
// the consumer has 1 warning, so it is the only YELLOW row in the tier mix.
assertEqual(
  "lambda tiers",
  lambdaSummary.map(logic.lambdaErrorTier),
  ["YELLOW", "GREEN", "GREEN"],
);
assertEqual(
  "lambda totals",
  [
    lambdaInsights.totalInvocations,
    lambdaInsights.totalServiceErrors,
    lambdaInsights.totalWarnings,
    lambdaInsights.totalErrors,
  ],
  [1166, 0, 1, 0],
);
assertEqual("worst tail latency", lambdaInsights.slowestLambda.lambda_name, "zen-prod-sqs-message-consumer");
assertEqual("coldest lambda", lambdaInsights.coldestLambda.lambda_name, "zen-prod-sqs-message-consumer");

// The default ignore rule means there are no top_errors rows in the CSV; the
// only surviving top-message entry should be the single WARNING the script
// kept after filtering should_flush.emergency_accumulation.
assertEqual("top messages count", topMessages.length, 1);
assertEqual("top message text", topMessages[0].message, "stream_runner.llm_streaming.failed");
assertEqual("top message level", topMessages[0].level, "WARNING");

// parseTopMessageList must correctly round-trip the encoded "msg:count; msg:count" grammar.
assertEqual(
  "parseTopMessageList encoding",
  logic.parseTopMessageList("first.event:3; second.event:5"),
  [
    { message: "first.event", count: 3 },
    { message: "second.event", count: 5 },
  ],
);

/* ------------------------------------------------------------------ */
/* Bridge integration checks: fetch is stubbed; we assert URL/headers */
/* and that the response normalizes into parseCsv-compatible rows.    */
/* ------------------------------------------------------------------ */

assertEqual("bridgeFetchUrl trims trailing slash", logic.bridgeFetchUrl("http://127.0.0.1:8765/", "/healthz"), "http://127.0.0.1:8765/healthz");
assertEqual("parseBrandIdsInput happy path", logic.parseBrandIdsInput("470, 257,  466"), [470, 257, 466]);

let parseError = null;
try { logic.parseBrandIdsInput("470, abc"); }
catch (err) { parseError = err.message; }
assertEqual("parseBrandIdsInput rejects non-int", !!parseError, true);

// Stub fetch so we can assert on the request and inject a canned body.
let lastFetchCall = null;
globalThis.fetch = async (url, init) => {
  lastFetchCall = { url, init };
  return {
    ok: true,
    status: 200,
    async json() {
      return {
        rows: [
          { brand_id: 470, brand_name: "Acme",
            total_sessions: 12, total_utterances: 180, avg_utterances_per_session: 15.0 },
          { brand_id: 257, brand_name: "Beta",
            total_sessions: 4, total_utterances: 20, avg_utterances_per_session: 5.0 },
        ],
        row_count: 2,
        fetched_at: "2026-05-14T09:00:00Z",
        days: 1,
        brand_ids: [470, 257],
      };
    },
  };
};

(async () => {
  const payload = await logic.fetchMysqlAdoption({
    url: "http://127.0.0.1:8765",
    token: "secret",
    days: 1,
    brandIds: [470, 257],
  });

  assertEqual("fetchMysqlAdoption hits the right path", lastFetchCall.url, "http://127.0.0.1:8765/query/mysql/adoption");
  assertEqual("fetchMysqlAdoption posts JSON", lastFetchCall.init.headers["Content-Type"], "application/json");
  assertEqual("fetchMysqlAdoption sends X-Bridge-Token", lastFetchCall.init.headers["X-Bridge-Token"], "secret");
  assertEqual("fetchMysqlAdoption returns rows", payload.rows.length, 2);

  // Normalize and confirm validateColumns(MYSQL_REQUIRED, ...) accepts the result.
  const normalized = logic.normalizeFetchedMysqlRows(payload.rows);
  logic.validateColumns(normalized, logic.MYSQL_REQUIRED, "normalized bridge rows");
  assertEqual(
    "normalized brand_id is string (parseCsv parity)",
    typeof normalized[0].brand_id,
    "string",
  );
  assertEqual("normalized brand_name", normalized[0].brand_name, "Acme");

  /* ----- Lambda bridge integration ----- */

  assertEqual(
    "parseLambdaNamesInput splits newlines and commas",
    logic.parseLambdaNamesInput("alpha,\nbeta,  ,gamma"),
    ["alpha", "beta", "gamma"],
  );
  assertEqual(
    "parseLambdaNamesInput returns empty for blank input",
    logic.parseLambdaNamesInput("   "),
    [],
  );

  let lambdaFetchCall = null;
  globalThis.fetch = async (url, init) => {
    lambdaFetchCall = { url, init };
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          rows: [
            { lambda_name: "zen-prod-sqs-message-consumer", invocations: 571,
              lambda_errors: 0, throttles: 0,
              avg_duration_ms: "760.15", p95_duration_ms: "3354.11",
              p99_duration_ms: "5222.44", max_duration_ms: "7015.35",
              cold_starts: 19, avg_init_duration_ms: "1605.81",
              warning_count: 1, error_count: 0,
              top_errors: "",
              top_warnings: "stream_runner.llm_streaming.failed:1" },
          ],
          row_count: 1,
          env: "prod", region: "us-west-1", days: 1,
          start_time: "2026-05-13T08:30:00Z",
          end_time: "2026-05-14T08:30:00Z",
          fetched_at: "2026-05-14T08:30:00Z",
          ignored_messages: ["should_flush.emergency_accumulation"],
        };
      },
    };
  };

  const lambdaPayload = await logic.fetchLambdaCloudwatch({
    url: "http://127.0.0.1:8765",
    token: "lambda-token",
    env: "prod",
    region: "us-west-1",
    days: 1,
    lambdaNames: ["zen-prod-sqs-message-consumer"],
    ignoreMessages: [],
    useDefaultIgnores: true,
  });

  assertEqual(
    "fetchLambdaCloudwatch hits the right path",
    lambdaFetchCall.url,
    "http://127.0.0.1:8765/query/lambda/cloudwatch",
  );
  assertEqual(
    "fetchLambdaCloudwatch sends bridge token",
    lambdaFetchCall.init.headers["X-Bridge-Token"],
    "lambda-token",
  );
  const lambdaBody = JSON.parse(lambdaFetchCall.init.body);
  assertEqual("fetchLambdaCloudwatch posts env", lambdaBody.env, "prod");
  assertEqual("fetchLambdaCloudwatch posts region", lambdaBody.region, "us-west-1");
  assertEqual("fetchLambdaCloudwatch posts lambda_names", lambdaBody.lambda_names, ["zen-prod-sqs-message-consumer"]);
  assertEqual("fetchLambdaCloudwatch posts use_default_ignores", lambdaBody.use_default_ignores, true);

  const lambdaNormalized = logic.normalizeFetchedLambdaRows(lambdaPayload.rows);
  logic.validateColumns(lambdaNormalized, logic.LAMBDA_REQUIRED, "normalized lambda rows");
  // After validateColumns the row is also runnable through normalizeLambdaRows
  // (the same function the dashboard uses for uploaded CSVs).
  const enriched = logic.normalizeLambdaRows(lambdaNormalized);
  assertEqual("lambda row count", enriched.length, 1);
  assertEqual("lambda name preserved", enriched[0].lambda_name, "zen-prod-sqs-message-consumer");
  assertEqual("lambda invocations coerced to number", enriched[0].invocations, 571);
  assertEqual(
    "lambda top_warnings parsed back to {message,count}",
    enriched[0].top_warnings,
    [{ message: "stream_runner.llm_streaming.failed", count: 1 }],
  );

  /* ----- Pinot bridge integration ----- */

  let pinotFetchCall = null;
  globalThis.fetch = async (url, init) => {
    pinotFetchCall = { url, init };
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          rows: [
            { brand_id: "411",
              first_total_requests: "18", first_avg_ttfb_ms: "2900.50",
              first_p50_ttfb_ms: "2400.00", first_p95_ttfb_ms: "6800.85",
              first_p99_ttfb_ms: "7400.00",
              followup_total_requests: "39", followup_avg_ttfb_ms: "2400.60",
              followup_p50_ttfb_ms: "2100.00", followup_p95_ttfb_ms: "6100.12",
              followup_p99_ttfb_ms: "6900.00" },
          ],
          row_count: 1, fetched_at: "2026-05-14T09:00:00Z",
          days: 1, window_ms: 86400000, brand_ids: [411],
          num_docs_scanned: 57, time_used_ms: 3,
        };
      },
    };
  };

  const pinotPayload = await logic.fetchPinotLatency({
    url: "http://127.0.0.1:8765",
    token: "bridge-secret",
    pinotToken: "fake-jwt",
    days: 1,
    brandIds: [411],
  });

  assertEqual(
    "fetchPinotLatency hits the right path",
    pinotFetchCall.url,
    "http://127.0.0.1:8765/query/pinot/latency",
  );
  assertEqual(
    "fetchPinotLatency sends bridge token",
    pinotFetchCall.init.headers["X-Bridge-Token"],
    "bridge-secret",
  );
  const pinotBody = JSON.parse(pinotFetchCall.init.body);
  assertEqual("fetchPinotLatency posts days", pinotBody.days, 1);
  assertEqual("fetchPinotLatency posts brand_ids", pinotBody.brand_ids, [411]);
  assertEqual("fetchPinotLatency posts pinot_auth_token", pinotBody.pinot_auth_token, "fake-jwt");

  const pinotNormalized = logic.normalizeFetchedPinotRows(pinotPayload.rows);
  logic.validateColumns(pinotNormalized, logic.PINOT_REQUIRED, "normalized pinot rows");
  assertEqual("pinot brand_id string type", typeof pinotNormalized[0].brand_id, "string");
  assertEqual("pinot brand_id value", pinotNormalized[0].brand_id, "411");
  assertEqual(
    "pinot first p95 string preserved",
    pinotNormalized[0].first_p95_ttfb_ms,
    "6800.85",
  );
  assertEqual(
    "pinot followup p95 string preserved",
    pinotNormalized[0].followup_p95_ttfb_ms,
    "6100.12",
  );

  /* ----- Worst-of-buckets classifyHealth ----- */

  const thresholds = {
    p95GreenMax: 5000, p95RedMin: 8000,
    p99GreenMax: 8000, p99RedMin: 15000,
  };
  // First-utterance is GREEN (p95=3000, p99=5000), follow-up is RED (p95=9000).
  // The row should roll up to RED.
  const mixed = {
    first_p95_ttfb_ms: 3000, first_p99_ttfb_ms: 5000,
    followup_p95_ttfb_ms: 9000, followup_p99_ttfb_ms: 13000,
  };
  assertEqual("worst-of-buckets is RED when followup is RED", logic.classifyHealth(mixed, thresholds), "RED");
  // Both GREEN -> GREEN.
  assertEqual(
    "both buckets GREEN -> GREEN",
    logic.classifyHealth(
      { first_p95_ttfb_ms: 3000, first_p99_ttfb_ms: 5000,
        followup_p95_ttfb_ms: 2000, followup_p99_ttfb_ms: 4000 },
      thresholds,
    ),
    "GREEN",
  );
  // First-utterance is YELLOW, follow-up is GREEN -> row is YELLOW.
  assertEqual(
    "first YELLOW + follow GREEN -> YELLOW",
    logic.classifyHealth(
      { first_p95_ttfb_ms: 6000, first_p99_ttfb_ms: 7000,
        followup_p95_ttfb_ms: 3000, followup_p99_ttfb_ms: 5000 },
      thresholds,
    ),
    "YELLOW",
  );
  assertEqual("worstHealth helper picks RED over YELLOW", logic.worstHealth("YELLOW", "RED"), "RED");
  assertEqual("worstHealth helper picks YELLOW over GREEN", logic.worstHealth("GREEN", "YELLOW"), "YELLOW");

  /* ----- Sample-size threshold (min sample) ----- */

  // Two brands: "Big" with N=200 (above) and "Tiny" with N=1 (below threshold 10).
  const synthMerged = [
    {
      brand_id: "1", brand_name: "Big",
      total_sessions: 50, total_utterances: 700, avg_utterances_per_session: 14,
      first_total_requests: 80, first_avg_ttfb_ms: 1500,
      first_p50_ttfb_ms: 1400, first_p95_ttfb_ms: 3000, first_p99_ttfb_ms: 4000,
      followup_total_requests: 120, followup_avg_ttfb_ms: 1100,
      followup_p50_ttfb_ms: 1000, followup_p95_ttfb_ms: 2500, followup_p99_ttfb_ms: 3500,
      total_requests: 200,
    },
    {
      brand_id: "2", brand_name: "Tiny",
      total_sessions: 1, total_utterances: 1, avg_utterances_per_session: 1,
      first_total_requests: 1, first_avg_ttfb_ms: 1600,
      first_p50_ttfb_ms: 1600, first_p95_ttfb_ms: 1600, first_p99_ttfb_ms: 1600,
      followup_total_requests: 0, followup_avg_ttfb_ms: NaN,
      followup_p50_ttfb_ms: NaN, followup_p95_ttfb_ms: NaN, followup_p99_ttfb_ms: NaN,
      total_requests: 1,
    },
  ];
  const synthSummary = logic.buildSummary(synthMerged, thresholds, 10);
  assertEqual("buildSummary stamps below_min_sample on tiny rows",
    synthSummary.find((r) => r.brand_name === "Tiny").below_min_sample, true);
  assertEqual("buildSummary keeps below_min_sample=false on healthy rows",
    synthSummary.find((r) => r.brand_name === "Big").below_min_sample, false);

  const { above, below } = logic.partitionBySample(synthSummary);
  assertEqual("partitionBySample.above keeps the high-N brand",
    above.map((r) => r.brand_name), ["Big"]);
  assertEqual("partitionBySample.below filters the low-N brand",
    below.map((r) => r.brand_name), ["Tiny"]);

  // Insights should be computed from above-threshold rows only - "slowest"
  // must NOT be the tiny brand even though its raw p95 is technically higher.
  const filteredInsights = logic.computeInsights(above);
  assertEqual("computeInsights picks the high-N brand as slowest",
    filteredInsights.slowest.brand_name, "Big");

  // Empty above-threshold case: threshold of 500 hides everything.
  const strict = logic.buildSummary(synthMerged, thresholds, 500);
  const strictParts = logic.partitionBySample(strict);
  assertEqual("strict threshold pushes all rows below the line",
    strictParts.above.length, 0);
  assertEqual("strict threshold preserves the original row count below",
    strictParts.below.length, 2);

  /* ----- Cohort comparison (rollout vs control) ----- */

  // Mixed cohort summary: two rollout brands and two control brands, each
  // above threshold so the comparison sees them all.
  const cohortSummary = [
    {
      brand_id: "411", brand_name: "Rollout A", cohort: "rollout",
      total_sessions: 100,
      first_total_requests: 100, first_p95_ttfb_ms: 3000, first_p99_ttfb_ms: 4000,
      followup_total_requests: 300, followup_p95_ttfb_ms: 2500, followup_p99_ttfb_ms: 3000,
      total_requests: 400, health_status: "GREEN", first_health: "GREEN", followup_health: "GREEN",
    },
    {
      brand_id: "470", brand_name: "Rollout B", cohort: "rollout",
      total_sessions: 40,
      first_total_requests: 100, first_p95_ttfb_ms: 5000, first_p99_ttfb_ms: 6000,
      followup_total_requests: 100, followup_p95_ttfb_ms: 4500, followup_p99_ttfb_ms: 5500,
      total_requests: 200, health_status: "YELLOW", first_health: "YELLOW", followup_health: "GREEN",
    },
    {
      brand_id: "367", brand_name: "Control A", cohort: "control",
      total_sessions: 200,
      first_total_requests: 400, first_p95_ttfb_ms: 2000, first_p99_ttfb_ms: 2500,
      followup_total_requests: 800, followup_p95_ttfb_ms: 1800, followup_p99_ttfb_ms: 2200,
      total_requests: 1200, health_status: "GREEN", first_health: "GREEN", followup_health: "GREEN",
    },
    {
      brand_id: "311", brand_name: "Control B", cohort: "control",
      total_sessions: 100,
      first_total_requests: 100, first_p95_ttfb_ms: 3000, first_p99_ttfb_ms: 3500,
      followup_total_requests: 100, followup_p95_ttfb_ms: 2700, followup_p99_ttfb_ms: 3100,
      total_requests: 200, health_status: "GREEN", first_health: "GREEN", followup_health: "GREEN",
    },
  ];
  const cmp = logic.computeCohortComparison(cohortSummary);
  // Rollout request-weighted first_p95: (3000*100 + 5000*100) / (100+100) = 4000 ms
  // Control request-weighted first_p95: (2000*400 + 3000*100) / (400+100) = 2200 ms
  assertEqual("rollout request-weighted first p95",
    Math.round(cmp.rollout.firstP95), 4000);
  assertEqual("control request-weighted first p95",
    Math.round(cmp.control.firstP95), 2200);
  assertEqual("first p95 delta = rollout - control",
    Math.round(cmp.firstDelta), 1800);
  assertEqual("rollout brand count", cmp.rollout.brandCount, 2);
  assertEqual("control brand count", cmp.control.brandCount, 2);
  assertEqual("rollout total requests", cmp.rollout.totalRequests, 600);
  assertEqual("control total requests", cmp.control.totalRequests, 1400);

  // No-cohort path: classic rollout-only fetch (no cohort labels on rows).
  const uncohorted = synthSummary.map((r) => ({ ...r, cohort: undefined }));
  assertEqual("computeCohortComparison returns null when no rows are tagged",
    logic.computeCohortComparison(uncohorted), null);

  // Cohort-but-only-rollout path: control list empty, all rows are rollout.
  const rolloutOnly = cohortSummary.filter((r) => r.cohort === "rollout");
  const onlyCmp = logic.computeCohortComparison(rolloutOnly);
  assertEqual("rollout-only comparison has no control side",
    onlyCmp.control, null);
  assertEqual("rollout-only comparison cannot compute delta",
    onlyCmp.firstDelta, null);

  /* ----- buildPinotOnlySummary keeps cohorts MySQL doesn't know about ----- */

  // Raw Pinot rows (parseCsv-shaped: dict-of-strings) for both a rollout and
  // a control brand. MySQL would only know about the rollout one.
  const rawPinotRows = [
    {
      brand_id: "411", cohort: "rollout",
      first_total_requests: "20", first_avg_ttfb_ms: "3000.00",
      first_p50_ttfb_ms: "2800.00", first_p95_ttfb_ms: "4500.00", first_p99_ttfb_ms: "5500.00",
      followup_total_requests: "80", followup_avg_ttfb_ms: "2500.00",
      followup_p50_ttfb_ms: "2200.00", followup_p95_ttfb_ms: "3800.00", followup_p99_ttfb_ms: "4400.00",
    },
    {
      brand_id: "367", cohort: "control",
      first_total_requests: "100", first_avg_ttfb_ms: "1800.00",
      first_p50_ttfb_ms: "1700.00", first_p95_ttfb_ms: "2500.00", first_p99_ttfb_ms: "3000.00",
      followup_total_requests: "300", followup_avg_ttfb_ms: "1600.00",
      followup_p50_ttfb_ms: "1500.00", followup_p95_ttfb_ms: "2200.00", followup_p99_ttfb_ms: "2700.00",
    },
  ];
  const pinotOnly = logic.buildPinotOnlySummary(rawPinotRows, thresholds, 10);
  assertEqual("buildPinotOnlySummary returns one row per input",
    pinotOnly.length, 2);
  // Fallback brand_name when MySQL is bypassed.
  const rolloutRow = pinotOnly.find((r) => r.cohort === "rollout");
  const controlRow = pinotOnly.find((r) => r.cohort === "control");
  assertEqual("rollout fallback brand_name uses #<id>", rolloutRow.brand_name, "#411");
  assertEqual("control fallback brand_name uses #<id>", controlRow.brand_name, "#367");
  assertEqual("rollout total_requests sums first + followup",
    rolloutRow.total_requests, 100);
  assertEqual("control total_requests sums first + followup",
    controlRow.total_requests, 400);
  // Cohort comparison built from raw Pinot rows must include the control
  // brand that MySQL doesn't have - this is the bug fix.
  const rawCmp = logic.computeCohortComparison(
    pinotOnly.filter((r) => !r.below_min_sample),
  );
  assertEqual("comparison sees both cohorts when computed from Pinot-only",
    rawCmp.control != null && rawCmp.rollout != null, true);
  // Request-weighted first p95:
  //   rollout: (4500 * 20) / 20 = 4500ms
  //   control: (2500 * 100) / 100 = 2500ms
  //   delta = 4500 - 2500 = 2000ms
  assertEqual("rollout first p95 from raw pinot",
    Math.round(rawCmp.rollout.firstP95), 4500);
  assertEqual("control first p95 from raw pinot",
    Math.round(rawCmp.control.firstP95), 2500);
  assertEqual("delta = rollout - control", Math.round(rawCmp.firstDelta), 2000);

  console.log("\nAll JS pipeline smoke checks pass.");
})().catch((err) => {
  console.error("FAIL: async smoke checks threw\n", err);
  process.exit(1);
});
