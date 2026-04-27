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

## Notes

- The collector skips missing Lambdas and missing log groups gracefully.
- `Lambda Errors = 0` does not mean the application path is healthy; logs and `REPORT` analysis are still important.
- The script requires AWS CLI access to the target account and region.
- Generated raw artifacts and local reports should stay ignored from Git and Cursor.
