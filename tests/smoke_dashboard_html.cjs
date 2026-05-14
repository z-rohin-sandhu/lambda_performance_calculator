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
            { brand_id: "411", total_requests: "57",
              avg_ttfb_ms: "2740.09", p50_ttfb_ms: "2381.00",
              p95_ttfb_ms: "6483.85", p99_ttfb_ms: "6701.36" },
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
  assertEqual("pinot p95 string preserved", pinotNormalized[0].p95_ttfb_ms, "6483.85");

  console.log("\nAll JS pipeline smoke checks pass.");
})().catch((err) => {
  console.error("FAIL: async smoke checks threw\n", err);
  process.exit(1);
});
