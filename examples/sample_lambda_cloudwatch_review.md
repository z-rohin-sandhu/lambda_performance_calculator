# Lambda CloudWatch Review

## Scope

Time window reviewed: `2026-04-20T00:00:00Z` to `2026-04-27T23:59:59Z`

Lambdas reviewed:
- `zen-qa2-sqs-message-consumer`
- `zen-qa2-ws-to-sqs-producer`
- `zen_qa2_authorizer_service`

## Executive Summary

The issue is isolated to `zen-qa2-sqs-message-consumer`.

- All three Lambdas were healthy at the Lambda service level: `0` Lambda `Errors`, `0` `Throttles`, and no concurrency pressure.
- `zen-qa2-ws-to-sqs-producer` and `zen_qa2_authorizer_service` look healthy and fast.
- `zen-qa2-sqs-message-consumer` is the only Lambda showing:
  - significant tail latency
  - application warning/error logs
  - expensive cold starts
  - LLM retry exhaustion and fallback behavior

Most likely RCA:
- cold starts are a real contributor for the consumer
- they do not fully explain the worst-case latency
- the primary runtime issue appears to be in the LLM streaming path and/or its downstream dependencies

## Key Findings

| Lambda | Invocations | Lambda Errors | Throttles | Avg Duration | P95 | P99 | Max | Cold Starts | Avg Init |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `zen-qa2-sqs-message-consumer` | 570 | 0 | 0 | 405.90 ms | 2055.32 ms | 5022.79 ms | 8783.58 ms | 14 | 1685.74 ms |
| `zen-qa2-ws-to-sqs-producer` | 570 | 0 | 0 | 25.51 ms | 102.07 ms | 153.47 ms | 181.02 ms | 9 | 516.28 ms |
| `zen_qa2_authorizer_service` | 13 | 0 | 0 | 3.24 ms | 8.42 ms | 8.42 ms | 8.42 ms | 10 | 180.84 ms |

Additional observations:
- Concurrency stayed effectively flat for all three Lambdas.
- The consumer only briefly reached concurrency `2`, so load-driven contention is unlikely.
- All Lambdas run in a VPC.
- Config looked broadly healthy: no environment errors, no reserved concurrency configured.

## Application Error Signals

Only `zen-qa2-sqs-message-consumer` emitted warning/error logs in the selected window.

Warning/error totals:
- `zen-qa2-sqs-message-consumer`: `10 WARNING`, `8 ERROR`
- `zen-qa2-ws-to-sqs-producer`: `0`
- `zen_qa2_authorizer_service`: `0`

Top recurring messages in the consumer:
- `WARNING stream_runner.llm_streaming.failed`: `8`
- `ERROR stream_runner.llm_streaming.all_retries_exhausted`: `2`
- `WARNING default_response.sending`: `2`
- `ERROR should_flush.emergency_accumulation`: `2`
- `ERROR stream_runner.llm_streaming.failed`: `2`
- `ERROR stream_runner.llm_retries_exhausted`: `2`

## RCA Summary

`zen-qa2-sqs-message-consumer` is the only Lambda with both performance degradation and application-level instability.

The evidence points to this sequence:

1. Requests enter the consumer successfully and do not fail at the Lambda service level.
2. Some requests hit expensive cold starts, adding roughly `1.7s` average init overhead.
3. During runtime, the LLM streaming path intermittently fails or stalls.
4. Retries are attempted, and some requests exhaust all retries.
5. Fallback/default responses are sent for some failed requests.
6. `should_flush.emergency_accumulation` suggests delayed chunk flushing or oversized buffered output in a subset of requests.

Conclusion:
- cold starts explain part of the latency
- LLM streaming failures and retry exhaustion explain the application warnings/errors
- the worst latency is most likely caused by a combination of initialization overhead plus runtime dependency/provider delays

## Recommended Next Steps

1. Inspect `stream_runner` retry behavior and failure logs around `stream_runner.llm_streaming.*`.
2. Review the consumer's LLM provider and downstream dependencies for intermittent latency or filtered/rejected responses.
3. Review startup work in the consumer for expensive initialization inside the Lambda execution path.
4. Inspect `should_flush.emergency_accumulation` to confirm whether chunk buffering is amplifying response latency.
5. If this path is latency-sensitive, consider mitigations for cold starts such as provisioned concurrency or reducing init-time work.

## Notes

- Lambda `Errors = 0` does not mean the application was healthy; the consumer handled failures internally and returned fallback responses in some cases.
- The reported memory values from the `REPORT` query appear to be raw reported units and should be treated as comparative signals unless separately normalized.
