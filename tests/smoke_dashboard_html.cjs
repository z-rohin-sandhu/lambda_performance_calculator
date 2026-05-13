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

const sandbox = {
  parseCsv: null,
  validateColumns: null,
  mergeOnBrandId: null,
  buildSummary: null,
  computeInsights: null,
};

// Strip the DOMContentLoaded bootstrap (no DOM here) and expose internals.
const cleanedSrc = scriptSrc
  .replace(/document\.addEventListener\("DOMContentLoaded"[\s\S]*\)\;\s*$/, "")
  .replace(/window\.addEventListener\("drag[\s\S]*?\)\;\s*$/m, "");

const exportSrc = `
${cleanedSrc}
module.exports = { parseCsv, validateColumns, mergeOnBrandId, buildSummary, computeInsights };
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

console.log("\nAll JS pipeline smoke checks pass.");
