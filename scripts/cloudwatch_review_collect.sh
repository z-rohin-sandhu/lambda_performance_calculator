#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/cloudwatch_review_collect.sh [options]

Options:
  --env <name>         Environment name. Default: prod
  --start <iso8601>    Start time in UTC, e.g. 2026-04-20T00:00:00Z
  --end <iso8601>      End time in UTC, e.g. 2026-04-27T23:59:59Z
  --region <name>      AWS region. Default: value of AWS_REGION or us-west-1
  --output-dir <path>  Output base directory. Default: artifacts/cloudwatch-review
  --lambda <name>      Custom Lambda name. Repeat to provide multiple Lambdas.
  --message-filter-key <key>
                       Message JSON key used to derive filtered latency metrics.
  --message-filter-value <value>
                       Message JSON value used to derive filtered latency metrics.
                       Repeat to aggregate multiple values together.
  --help               Show this help text

Defaults:
  env   = prod
  end   = current UTC time
  start = one week before current UTC time

Default Lambdas:
  zen-{env}-sqs-message-consumer
  zen-{env}-ws-to-sqs-producer
  zen_{env}_authorizer_service
  zen_{env}_practice_lrs_event_publisher
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

default_end_time() {
  python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

default_start_time() {
  python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

to_epoch() {
  local iso_value="$1"
  python3 - "$iso_value" <<'PY'
from datetime import datetime
import sys

raw = sys.argv[1]
normalized = raw.replace("Z", "+00:00")
print(int(datetime.fromisoformat(normalized).timestamp()))
PY
}

sanitize_for_path() {
  local value="$1"
  value="${value//:/-}"
  value="${value// /_}"
  echo "${value//\//-}"
}

escape_for_regex() {
  local value="$1"
  python3 - "$value" <<'PY'
import re
import sys

print(re.escape(sys.argv[1]))
PY
}

escape_for_double_quotes() {
  local value="$1"
  python3 - "$value" <<'PY'
import sys

print(sys.argv[1].replace("\\", "\\\\").replace('"', '\\"'))
PY
}

join_with_delimiter() {
  local delimiter="$1"
  shift
  local joined_value=""
  local value=""

  for value in "$@"; do
    if [[ -n "${joined_value}" ]]; then
      joined_value+="${delimiter}"
    fi
    joined_value+="${value}"
  done

  printf '%s' "${joined_value}"
}

append_command_header() {
  local output_file="$1"
  local title="$2"
  {
    echo "### ${title}"
    echo
  } >> "${output_file}"
}

append_separator() {
  local output_file="$1"
  {
    echo
    echo "------------------------------------------------------------"
    echo
  } >> "${output_file}"
}

get_exact_log_group() {
  local lambda_name="$1"
  aws logs describe-log-groups \
    --region "${AWS_REGION}" \
    --log-group-name-prefix "/aws/lambda/${lambda_name}" \
    --query "logGroups[?logGroupName=='/aws/lambda/${lambda_name}'].logGroupName | [0]" \
    --output text 2>/dev/null || true
}

run_logs_query() {
  local output_file="$1"
  local title="$2"
  local query_string="$3"
  local query_id=""
  local status=""
  local response=""
  local attempts=0
  local max_attempts=30

  append_command_header "${output_file}" "${title}"

  if [[ "${#LOG_GROUPS[@]}" -eq 0 ]]; then
    {
      echo "No log groups were found for the selected Lambdas."
      echo
    } >> "${output_file}"
    return 0
  fi

  query_id="$(
    aws logs start-query \
      --region "${AWS_REGION}" \
      --log-group-names "${LOG_GROUPS[@]}" \
      --start-time "${START_EPOCH}" \
      --end-time "${END_EPOCH}" \
      --query-string "${query_string}" \
      --query "queryId" \
      --output text
  )"

  while (( attempts < max_attempts )); do
    response="$(aws logs get-query-results --region "${AWS_REGION}" --query-id "${query_id}" --output json)"
    status="$(
      RESPONSE_JSON="${response}" python3 -c 'import json, os; print(json.loads(os.environ["RESPONSE_JSON"]).get("status", ""))'
    )"

    if [[ "${status}" == "Complete" ]]; then
      break
    fi

    if [[ "${status}" == "Failed" || "${status}" == "Cancelled" || "${status}" == "Timeout" ]]; then
      break
    fi

    attempts=$((attempts + 1))
    sleep 2
  done

  {
    echo "QueryId: ${query_id}"
    echo "Status: ${status}"
    echo
    echo "${response}"
  } >> "${output_file}"
}

build_filtered_report_query() {
  local filter_key="$1"
  local escaped_filter_key=""
  local filter_expression=""
  local filter_value=""
  local escaped_filter_value=""

  escaped_filter_key="$(escape_for_regex "${filter_key}")"
  for filter_value in "${@:2}"; do
    escaped_filter_value="$(escape_for_double_quotes "${filter_value}")"
    if [[ -n "${filter_expression}" ]]; then
      filter_expression+=" or "
    fi
    filter_expression+="matched_filter_value = \"${escaped_filter_value}\""
  done

  cat <<EOF
fields @log, @requestId, @message, @duration
| parse @message /"${escaped_filter_key}"\s*:\s*"?(?<matched_value>[^",}\s]+)"?/
| stats
    max(@duration) as duration_ms,
    latest(matched_value) as matched_filter_value
  by @log, @requestId
| filter ispresent(matched_filter_value) and (${filter_expression})
| stats
    count(*) as matching_invocations,
    avg(duration_ms) as avg_duration_ms,
    pct(duration_ms, 90) as p90_duration_ms,
    pct(duration_ms, 95) as p95_duration_ms,
    pct(duration_ms, 99) as p99_duration_ms,
    max(duration_ms) as max_duration_ms
  by @log
| sort matching_invocations desc
EOF
}

build_context_file() {
  local context_file="$1"
  shift

  : > "${context_file}"

  for file_path in "$@"; do
    {
      echo "# File: $(basename "${file_path}")"
      echo
      cat "${file_path}"
      echo
      echo
    } >> "${context_file}"
  done
}

ENV_NAME="prod"
AWS_REGION="${AWS_REGION:-us-west-1}"
START_TIME="${START_TIME:-$(default_start_time)}"
END_TIME="${END_TIME:-$(default_end_time)}"
OUTPUT_BASE_DIR="${repo_root}/artifacts/cloudwatch-review"
MESSAGE_FILTER_KEY=""
declare -a MESSAGE_FILTER_VALUES=()

declare -a CUSTOM_LAMBDAS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV_NAME="$2"
      shift 2
      ;;
    --start)
      START_TIME="$2"
      shift 2
      ;;
    --end)
      END_TIME="$2"
      shift 2
      ;;
    --region)
      AWS_REGION="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_BASE_DIR="$2"
      shift 2
      ;;
    --lambda)
      CUSTOM_LAMBDAS+=("$2")
      shift 2
      ;;
    --message-filter-key)
      MESSAGE_FILTER_KEY="$2"
      shift 2
      ;;
    --message-filter-value)
      MESSAGE_FILTER_VALUES+=("$2")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -n "${MESSAGE_FILTER_KEY}" && "${#MESSAGE_FILTER_VALUES[@]}" -eq 0 ]]; then
  echo "--message-filter-value is required when --message-filter-key is set." >&2
  exit 1
fi

if [[ -z "${MESSAGE_FILTER_KEY}" && "${#MESSAGE_FILTER_VALUES[@]}" -gt 0 ]]; then
  echo "--message-filter-key is required when --message-filter-value is set." >&2
  exit 1
fi

declare -a REQUESTED_LAMBDAS=()
if [[ "${#CUSTOM_LAMBDAS[@]}" -gt 0 ]]; then
  REQUESTED_LAMBDAS=("${CUSTOM_LAMBDAS[@]}")
else
  REQUESTED_LAMBDAS=(
    "zen-${ENV_NAME}-sqs-message-consumer"
    "zen-${ENV_NAME}-ws-to-sqs-producer"
    "zen_${ENV_NAME}_authorizer_service"
    "zen_${ENV_NAME}_practice_lrs_event_publisher"
  )
fi

START_EPOCH="$(to_epoch "${START_TIME}")"
END_EPOCH="$(to_epoch "${END_TIME}")"

run_id="${ENV_NAME}-$(sanitize_for_path "${START_TIME}")_$(sanitize_for_path "${END_TIME}")"
run_dir="${OUTPUT_BASE_DIR}/${run_id}"
mkdir -p "${run_dir}"

metadata_file="${run_dir}/metadata.txt"
config_file="${run_dir}/config.txt"
metrics_invocations_file="${run_dir}/metrics_invocations.txt"
metrics_duration_file="${run_dir}/metrics_duration.txt"
metrics_concurrency_file="${run_dir}/metrics_concurrency.txt"
logs_error_counts_file="${run_dir}/logs_error_counts.txt"
logs_top_messages_file="${run_dir}/logs_top_messages.txt"
logs_report_file="${run_dir}/logs_report.txt"
logs_filtered_report_file="${run_dir}/logs_filtered_report.txt"
context_file="${run_dir}/context.txt"

declare -a ACTIVE_LAMBDAS=()
declare -a MISSING_LAMBDAS=()
declare -a LOG_GROUPS=()

{
  echo "env=${ENV_NAME}"
  echo "region=${AWS_REGION}"
  echo "start_time=${START_TIME}"
  echo "end_time=${END_TIME}"
  echo "start_epoch=${START_EPOCH}"
  echo "end_epoch=${END_EPOCH}"
  echo "run_id=${run_id}"
  echo "run_dir=${run_dir}"
  echo "message_filter_key=${MESSAGE_FILTER_KEY}"
  echo
  echo "[message_filter_values]"
  if [[ "${#MESSAGE_FILTER_VALUES[@]}" -gt 0 ]]; then
    printf '%s\n' "${MESSAGE_FILTER_VALUES[@]}"
  else
    echo "none"
  fi
  echo
  echo "[requested_lambdas]"
  printf '%s\n' "${REQUESTED_LAMBDAS[@]}"
} > "${metadata_file}"

: > "${config_file}"
append_command_header "${config_file}" "Lambda configuration"

for lambda_name in "${REQUESTED_LAMBDAS[@]}"; do
  {
    echo "Lambda: ${lambda_name}"
    echo
  } >> "${config_file}"

  if aws lambda get-function-configuration \
    --region "${AWS_REGION}" \
    --function-name "${lambda_name}" \
    --output json >> "${config_file}" 2>&1; then
    ACTIVE_LAMBDAS+=("${lambda_name}")
  else
    MISSING_LAMBDAS+=("${lambda_name}")
  fi

  append_separator "${config_file}"
done

{
  echo
  echo "[active_lambdas]"
  printf '%s\n' "${ACTIVE_LAMBDAS[@]}"
  echo
  echo "[missing_lambdas]"
  if [[ "${#MISSING_LAMBDAS[@]}" -gt 0 ]]; then
    printf '%s\n' "${MISSING_LAMBDAS[@]}"
  else
    echo "none"
  fi
} >> "${metadata_file}"

for lambda_name in "${ACTIVE_LAMBDAS[@]}"; do
  log_group="$(get_exact_log_group "${lambda_name}")"
  if [[ -n "${log_group}" && "${log_group}" != "None" ]]; then
    LOG_GROUPS+=("${log_group}")
  fi
done

{
  echo
  echo "[log_groups]"
  if [[ "${#LOG_GROUPS[@]}" -gt 0 ]]; then
    printf '%s\n' "${LOG_GROUPS[@]}"
  else
    echo "none"
  fi
} >> "${metadata_file}"

: > "${metrics_invocations_file}"
append_command_header "${metrics_invocations_file}" "Invocations, errors, and throttles"
for lambda_name in "${ACTIVE_LAMBDAS[@]}"; do
  {
    echo "Lambda: ${lambda_name}"
    echo "-- Invocations --"
  } >> "${metrics_invocations_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name Invocations \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 86400 \
    --statistics Sum \
    --output json >> "${metrics_invocations_file}" 2>&1
  {
    echo
    echo "-- Errors --"
  } >> "${metrics_invocations_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name Errors \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 86400 \
    --statistics Sum \
    --output json >> "${metrics_invocations_file}" 2>&1
  {
    echo
    echo "-- Throttles --"
  } >> "${metrics_invocations_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name Throttles \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 86400 \
    --statistics Sum \
    --output json >> "${metrics_invocations_file}" 2>&1
  append_separator "${metrics_invocations_file}"
done

: > "${metrics_duration_file}"
append_command_header "${metrics_duration_file}" "Duration metrics"
for lambda_name in "${ACTIVE_LAMBDAS[@]}"; do
  {
    echo "Lambda: ${lambda_name}"
    echo "-- Duration average and maximum --"
  } >> "${metrics_duration_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name Duration \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 3600 \
    --statistics Average Maximum \
    --output json >> "${metrics_duration_file}" 2>&1
  {
    echo
    echo "-- Duration p95 --"
  } >> "${metrics_duration_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name Duration \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 3600 \
    --extended-statistics p95 \
    --output json >> "${metrics_duration_file}" 2>&1
  append_separator "${metrics_duration_file}"
done

: > "${metrics_concurrency_file}"
append_command_header "${metrics_concurrency_file}" "Concurrency metrics"
for lambda_name in "${ACTIVE_LAMBDAS[@]}"; do
  {
    echo "Lambda: ${lambda_name}"
  } >> "${metrics_concurrency_file}"
  aws cloudwatch get-metric-statistics \
    --region "${AWS_REGION}" \
    --namespace AWS/Lambda \
    --metric-name ConcurrentExecutions \
    --dimensions Name=FunctionName,Value="${lambda_name}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 3600 \
    --statistics Maximum Average \
    --output json >> "${metrics_concurrency_file}" 2>&1
  append_separator "${metrics_concurrency_file}"
done

error_counts_query='fields @log, @message
| parse @message /"level"\s*:\s*"(?<level>[^"]+)"/
| filter level in ["ERROR", "WARNING"]
| stats count(*) as total by @log, level
| sort total desc'

top_messages_query='fields @timestamp, @log, @message
| parse @message /"level"\s*:\s*"(?<level>[^"]+)"/
| parse @message /"message"\s*:\s*"(?<app_message>[^"]+)"/
| filter level in ["ERROR", "WARNING"]
| stats count() as total by @log, level, app_message
| sort total desc
| limit 100'

report_query='filter @type = "REPORT"
| stats
    count(*) as invocations,
    avg(@duration) as avg_duration_ms,
    pct(@duration, 95) as p95_duration_ms,
    pct(@duration, 99) as p99_duration_ms,
    max(@duration) as max_duration_ms,
    avg(@billedDuration) as avg_billed_duration_ms,
    avg(@maxMemoryUsed) as avg_memory_used_mb,
    max(@maxMemoryUsed) as max_memory_used_mb,
    count(@initDuration) as cold_starts,
    avg(@initDuration) as avg_init_duration_ms,
    max(@initDuration) as max_init_duration_ms
  by @log
| sort invocations desc'

run_logs_query "${logs_error_counts_file}" "Warning and error counts by Lambda" "${error_counts_query}"
run_logs_query "${logs_top_messages_file}" "Top warning and error messages" "${top_messages_query}"
run_logs_query "${logs_report_file}" "Lambda REPORT analysis" "${report_query}"

if [[ -n "${MESSAGE_FILTER_KEY}" ]]; then
  rendered_filter_values="$(join_with_delimiter ", " "${MESSAGE_FILTER_VALUES[@]}")"
  filtered_report_query="$(build_filtered_report_query "${MESSAGE_FILTER_KEY}" "${MESSAGE_FILTER_VALUES[@]}")"
  run_logs_query \
    "${logs_filtered_report_file}" \
    "Lambda REPORT analysis filtered by ${MESSAGE_FILTER_KEY} in [${rendered_filter_values}]" \
    "${filtered_report_query}"
else
  : > "${logs_filtered_report_file}"
  append_command_header "${logs_filtered_report_file}" "Filtered Lambda REPORT analysis"
  {
    echo "No message filter was requested."
    echo
  } >> "${logs_filtered_report_file}"
fi

build_context_file \
  "${context_file}" \
  "${metadata_file}" \
  "${config_file}" \
  "${metrics_invocations_file}" \
  "${metrics_duration_file}" \
  "${metrics_concurrency_file}" \
  "${logs_error_counts_file}" \
  "${logs_top_messages_file}" \
  "${logs_report_file}" \
  "${logs_filtered_report_file}"

echo "CloudWatch review artifacts created at:"
echo "${run_dir}"
echo
echo "Combined context file:"
echo "${context_file}"
