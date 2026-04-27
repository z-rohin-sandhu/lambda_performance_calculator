Use `docs/templates/lambda_cloudwatch_review.template.md` as the output format.

Input sources:
- The template markdown file
- `artifacts/cloudwatch-review/<run-id>/metadata.txt`
- `artifacts/cloudwatch-review/<run-id>/config.txt`
- `artifacts/cloudwatch-review/<run-id>/metrics_invocations.txt`
- `artifacts/cloudwatch-review/<run-id>/metrics_duration.txt`
- `artifacts/cloudwatch-review/<run-id>/metrics_concurrency.txt`
- `artifacts/cloudwatch-review/<run-id>/logs_error_counts.txt`
- `artifacts/cloudwatch-review/<run-id>/logs_top_messages.txt`
- `artifacts/cloudwatch-review/<run-id>/logs_report.txt`
- `artifacts/cloudwatch-review/<run-id>/context.txt`

Task:
1. Read the template and replace every placeholder with real content derived from the artifact files.
2. Keep the final report concise, crisp, and written for engineers and stakeholders.
3. Do not paste raw CLI output unless absolutely necessary.
4. Prefer synthesized findings over long data dumps.
5. If a metric is missing, say it is unavailable instead of guessing.
6. If one Lambda is clearly problematic, make that the center of the executive summary and RCA.
7. If all Lambdas are healthy, say that clearly.
8. Keep the tone factual and specific.

Formatting guidance:
- Use short paragraphs and flat bullets.
- Keep the executive summary to 3-6 bullets.
- Keep the "Additional observations" section brief.
- Limit "Top recurring messages" to the most meaningful signals.
- Make "Recommended Next Steps" action-oriented and prioritized.

Placeholder guidance:
- `{{LAMBDA_LIST}}`: markdown bullets with Lambda names
- `{{KEY_METRICS_TABLE}}`: one markdown table row per Lambda
- `{{ADDITIONAL_OBSERVATIONS}}`: flat bullets
- `{{ERROR_SUMMARY}}`: short paragraph or bullets summarizing warning/error counts by Lambda
- `{{TOP_MESSAGES}}`: flat bullets with the most important recurring messages
- `{{EXECUTIVE_SUMMARY}}`, `{{RCA_SUMMARY}}`, `{{NEXT_STEPS}}`, and `{{NOTES}}`: concise prose or flat bullets

Output:
- Save the final rendered markdown report under `reports/` using the filename convention `reports/{env}_{execution_date}_websocket_reports.md`.
- Use `YYYY-MM-DD` for `execution_date`, derived from the review run metadata unless the user specifies a different date.
- Return only the final rendered markdown report.
