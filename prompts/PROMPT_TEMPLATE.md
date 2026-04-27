# CloudWatch Review Prompt Template

Use `@docs/templates/lambda_cloudwatch_review.template.md` and `@prompts/fill_lambda_cloudwatch_review.prompt.md`.

Find the latest artifact folder under `@artifacts/cloudwatch-review/` for environment `<env>` and use these files from that folder:
- `metadata.txt`
- `config.txt`
- `metrics_invocations.txt`
- `metrics_duration.txt`
- `metrics_concurrency.txt`
- `logs_error_counts.txt`
- `logs_top_messages.txt`
- `logs_report.txt`
- `context.txt`

Generate the final markdown report using the template and save it as:
`reports/<env>_<execution_date>_websocket_reports.md`

Use `YYYY-MM-DD` for `execution_date`, derived from the artifact metadata.

Return only the final rendered markdown.
