# Lambda Performance Calculator

Reusable workflow for collecting AWS Lambda CloudWatch data and turning it into a concise performance/RCA report.

## What this repo contains

- `scripts/cloudwatch_review_collect.sh`
  Collects Lambda configuration, metrics, log counts, top messages, and `REPORT` analysis into artifact files.
- `scripts/run_cloudwatch_review.py`
  Interactive automation that runs the collector, parses the generated artifact files, and writes the final markdown report.
- `docs/templates/lambda_cloudwatch_review.template.md`
  Generic markdown template for the final report.
- `prompts/fill_lambda_cloudwatch_review.prompt.md`
  Prompt instructions for turning collected artifact files into the final markdown report.
- `examples/sample_lambda_cloudwatch_review.md`
  Sample final report output.
- `scripts/gpt_rollout_health_dashboard.py`
  Standalone dashboard generator that merges a Pinot latency CSV and a MySQL adoption CSV into a `merged_metrics.csv` + `dashboard.png` for daily GPT rollout health monitoring.
- `dashboards/gpt_rollout_dashboard.html`
  Self-contained drag-and-drop browser version of the dashboard (same logic, runs 100% client-side, downloads PNG + CSV).
- `examples/sample_pinot_metrics.csv`, `examples/sample_mysql_metrics.csv`
  Sample inputs for the GPT rollout health dashboard (work with both the Python and HTML versions).

## Default behavior

If you run the collector without arguments, it uses:

- `env=prod`
- `end=current UTC time`
- `start=one week before current UTC time`
- default Lambdas:
  - `zen-{env}-sqs-message-consumer`
  - `zen-{env}-ws-to-sqs-producer`
  - `zen_{env}_authorizer_service`
  - `zen_{env}_practice_lrs_event_publisher`

## Step 1: Collect CloudWatch data

From the project root:

```bash
cd "/Users/rohinsandhu/Documents/zenarate/lambda_performance_calculator"
./scripts/cloudwatch_review_collect.sh
```

Example for another environment:

```bash
./scripts/cloudwatch_review_collect.sh --env qa2
```

Example with explicit dates:

```bash
./scripts/cloudwatch_review_collect.sh \
  --env beta \
  --start 2026-04-20T00:00:00Z \
  --end 2026-04-27T23:59:59Z
```

Example with custom Lambda names:

```bash
./scripts/cloudwatch_review_collect.sh \
  --env dev \
  --lambda zen-dev-sqs-message-consumer \
  --lambda zen-dev-ws-to-sqs-producer \
  --lambda zen_dev_authorizer_service
```

## Step 2: Use the generated artifacts to create the report

After the script finishes, it creates a folder like:

```text
artifacts/cloudwatch-review/<run-id>/
```

Important files inside:

- `metadata.txt`
- `config.txt`
- `metrics_invocations.txt`
- `metrics_duration.txt`
- `metrics_concurrency.txt`
- `logs_error_counts.txt`
- `logs_top_messages.txt`
- `logs_report.txt`
- `context.txt`

Use these with:

- template: `docs/templates/lambda_cloudwatch_review.template.md`
- prompt: `prompts/fill_lambda_cloudwatch_review.prompt.md`

The final markdown output should be saved locally under:

```text
reports/{env}_{execution_date}_websocket_reports.md
```

Example:

```text
reports/prod_2026-04-27_websocket_reports.md
```

Recommended input set for the AI step:

1. `docs/templates/lambda_cloudwatch_review.template.md`
2. `prompts/fill_lambda_cloudwatch_review.prompt.md`
3. `artifacts/cloudwatch-review/<run-id>/context.txt`

Or, if needed, provide the individual artifact files listed in the prompt.

## Step 3: Run the full automation

No external Python package is required. The automation uses the standard library only.

You can optionally create a local `.env` file from `.env.example` to provide defaults such as `AWS_REGION`. The automation auto-loads `.env` from the repo root.

Run the interactive workflow:

```bash
python3 scripts/run_cloudwatch_review.py
```

The script will:

1. prompt for environment, region, time window, and Lambda names
2. run `scripts/cloudwatch_review_collect.sh`
3. parse the generated artifact files directly
4. fill the markdown template deterministically
5. save the report to `reports/{env}_{execution_date}_websocket_reports.md`

## Output expectation

The final generated report should be a concise markdown file covering:

- scope
- executive summary
- key findings
- application error signals
- RCA summary
- recommended next steps

## GPT rollout health dashboard

`scripts/gpt_rollout_health_dashboard.py` is a small, standalone tool meant to be run daily after exporting fresh CSVs from Pinot (latency) and MySQL (adoption). It does not connect to any database — CSV in, dashboard + CSV out.

Install runtime dependencies (pandas + matplotlib) once:

```bash
pip install -r requirements.txt
```

Run against the bundled samples:

```bash
python3 scripts/gpt_rollout_health_dashboard.py \
  --pinot-csv examples/sample_pinot_metrics.csv \
  --mysql-csv examples/sample_mysql_metrics.csv \
  --output-dir reports/gpt_rollout/$(date -u +%F)
```

Outputs (placed inside `--output-dir`):

- `merged_metrics.csv` — merged + health-classified summary, sorted by `p95_ttfb_ms` descending.
- `dashboard.png` — 3-panel matplotlib dashboard (p95 bar chart, sessions bar chart, adoption-vs-latency scatter) with GREEN / YELLOW / RED coloring.

Default `health_status` rules:

- `GREEN`  — `p95_ttfb_ms < 5000` AND `p99_ttfb_ms < 8000`
- `RED`    — `p95_ttfb_ms > 8000` OR `p99_ttfb_ms > 15000`
- `YELLOW` — everything else (typical warning zone)

Thresholds can be tightened or relaxed at the CLI:

```bash
python3 scripts/gpt_rollout_health_dashboard.py \
  --pinot-csv pinot.csv --mysql-csv mysql.csv \
  --p95-green-max-ms 3000 --p95-red-min-ms 6000 \
  --p99-green-max-ms 6000 --p99-red-min-ms 12000
```

Expected input columns:

- Pinot CSV: `brand_id, total_requests, avg_ttfb_ms, p50_ttfb_ms, p95_ttfb_ms, p99_ttfb_ms`
- MySQL CSV: `brand_id, brand_name, total_sessions, total_utterances, avg_utterances_per_session`

### Browser version (drag & drop)

If you don't want to set up Python, just open `dashboards/gpt_rollout_dashboard.html` in any modern browser:

```bash
open dashboards/gpt_rollout_dashboard.html
```

- Drag the Pinot CSV onto the left drop zone and the MySQL CSV onto the right one (or click to browse).
- Optionally tweak the health thresholds.
- Click **Generate dashboard** to see insights cards, a colored summary table, and the rendered chart.
- Click **Download dashboard.png** for the image and **Download merged_metrics.csv** for the merged data.

The page is fully self-contained (no external dependencies, no servers, no uploads — files never leave your machine). The merge, health classification, sort, and insights logic mirror `scripts/gpt_rollout_health_dashboard.py` exactly; the Node smoke test in `tests/smoke_dashboard_html.cjs` enforces parity on the sample CSVs.

### Deploy the dashboard to S3

Because the dashboard is a single self-contained HTML file, you can host it on S3 in seconds.

```bash
# 1. Quick private deploy + presigned URL valid for 24h (works on any bucket
#    you can write to; no public-read permissions required).
./scripts/deploy_dashboard_to_s3.sh --bucket my-internal-tools

# 2. Public object (bucket must allow public ACLs).
./scripts/deploy_dashboard_to_s3.sh --bucket my-public-tools --public

# 3. Full static-website hosting (idempotent one-time bucket setup).
./scripts/deploy_dashboard_to_s3.sh --bucket my-public-tools --public --website

# Common extras:
./scripts/deploy_dashboard_to_s3.sh \
  --bucket my-internal-tools \
  --prefix gpt-rollout/2026-05-13/ \
  --region us-west-1 \
  --profile zen-prod \
  --expires-seconds 604800 \
  --samples
```

The script:

- Uploads `dashboards/gpt_rollout_dashboard.html` (and optional sample CSVs) with `Content-Type: text/html; charset=utf-8` and `Cache-Control: no-cache` so daily redeploys are picked up immediately.
- Prints the `s3://` URI, the canonical HTTPS object URL, and a **presigned URL** (default 24h, max 7 days) you can paste into Slack.
- With `--public`, also sets the `public-read` ACL and prints the unsigned object URL.
- With `--website`, applies a static-website configuration and a scoped public-read bucket policy (skipped if a bucket policy already exists) and prints the website endpoint URL.
- Honors `AWS_REGION`, `AWS_PROFILE`, `S3_BUCKET`, and `S3_PREFIX` from your environment / `.env` file.
- Supports `--dry-run` to preview every `aws` command without executing it.

Requirements: AWS CLI v2 and credentials with `s3:PutObject` (plus `s3:PutBucketWebsite`, `s3:PutBucketPolicy`, `s3:PutObjectAcl` if you use `--public` / `--website`).

## Notes

- The collector skips missing Lambdas and missing log groups gracefully.
- `Lambda Errors = 0` does not mean the application path is healthy; logs and `REPORT` analysis are still important.
- The script requires AWS CLI access to the target account and region.
- Generated raw artifacts and local reports should stay ignored from Git and Cursor.
