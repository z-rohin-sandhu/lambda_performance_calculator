# Lambda CloudWatch Review

## Scope

Time window reviewed: `{{START_TIME}}` to `{{END_TIME}}`

Environment reviewed: `{{ENV_NAME}}`

Lambdas reviewed:
{{LAMBDA_LIST}}

## Executive Summary

{{EXECUTIVE_SUMMARY}}

## Key Findings

| Lambda | Invocations | Lambda Errors | Throttles | Avg Duration | P95 | P99 | Max | Cold Starts | Avg Init |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{{KEY_METRICS_TABLE}}

Filtered latency metrics:
{{FILTERED_METRICS}}

Additional observations:
{{ADDITIONAL_OBSERVATIONS}}

## Application Error Signals

{{ERROR_SUMMARY}}

Top recurring messages:
{{TOP_MESSAGES}}

## RCA Summary

{{RCA_SUMMARY}}

## Recommended Next Steps

{{NEXT_STEPS}}

## Notes

{{NOTES}}
