#!/usr/bin/env bash

# Deploy the GPT rollout dashboard HTML to an S3 bucket and print sharable URLs.
#
# Default mode: upload the file privately and print a presigned URL (works on
# any bucket you can write to, no public read permissions required).
#
# --public:  also try to make the object publicly readable via ACL and print
#            the canonical https://bucket.s3.region.amazonaws.com/key URL.
# --website: additionally apply a static-website configuration to the bucket
#            (index document = the dashboard file) and print the website URL.
#            Requires bucket-level permissions and that "Block Public Access"
#            is configured to allow public bucket policies.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy_dashboard_to_s3.sh --bucket <name> [options]

Required:
  --bucket <name>         S3 bucket to deploy into (or set S3_BUCKET env var).

Options:
  --prefix <key-prefix>   Key prefix inside the bucket. Default: gpt-rollout-dashboard/
  --region <name>         AWS region. Default: value of AWS_REGION or us-west-1.
  --profile <name>        AWS named profile. Default: AWS_PROFILE or default credentials.
  --public                Make the uploaded object public-read via ACL and print the object URL.
  --website               Configure the bucket for static website hosting (idempotent)
                          using the dashboard as index document. Prints the website URL.
  --expires-seconds <n>   Presigned URL lifetime in seconds. Default: 86400 (24h). Max: 604800 (7d).
  --samples               Also upload the bundled example CSVs alongside the dashboard.
  --dashboard-file <path> Override the HTML file to upload.
                          Default: dashboards/gpt_rollout_dashboard.html
  --no-cache              Set Cache-Control: no-cache (default). Use --cache to use defaults.
  --cache                 Do not set Cache-Control (let S3 use its defaults).
  --dry-run               Print the actions without executing aws CLI commands.
  --help                  Show this help text.

Environment variables:
  S3_BUCKET, S3_PREFIX, AWS_REGION, AWS_PROFILE are honored if their CLI flags are not set.

Examples:
  # Quick private deploy + presigned URL valid for 24h.
  ./scripts/deploy_dashboard_to_s3.sh --bucket my-internal-tools

  # Public object deploy (bucket must allow public ACLs).
  ./scripts/deploy_dashboard_to_s3.sh --bucket my-public-tools --public

  # Full static-website hosting setup (one-time bucket configuration).
  ./scripts/deploy_dashboard_to_s3.sh --bucket my-public-tools --public --website
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

# Reuse the .env loader pattern from the rest of the repo.
load_env_file() {
  local env_path="${repo_root}/.env"
  [[ -f "$env_path" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$env_path"
  set +a
}

# ----- Argument parsing ------------------------------------------------------

bucket="${S3_BUCKET:-}"
prefix_arg=""
region_arg=""
profile_arg=""
make_public=false
configure_website=false
expires_seconds=86400
upload_samples=false
dashboard_file="${repo_root}/dashboards/gpt_rollout_dashboard.html"
use_no_cache=true
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)            bucket="$2"; shift 2 ;;
    --prefix)            prefix_arg="$2"; shift 2 ;;
    --region)            region_arg="$2"; shift 2 ;;
    --profile)           profile_arg="$2"; shift 2 ;;
    --public)            make_public=true; shift ;;
    --website)           configure_website=true; shift ;;
    --expires-seconds)   expires_seconds="$2"; shift 2 ;;
    --samples)           upload_samples=true; shift ;;
    --dashboard-file)    dashboard_file="$2"; shift 2 ;;
    --no-cache)          use_no_cache=true; shift ;;
    --cache)             use_no_cache=false; shift ;;
    --dry-run)           dry_run=true; shift ;;
    --help|-h)           usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

load_env_file

if [[ -z "$bucket" ]]; then
  echo "Error: --bucket is required (or set S3_BUCKET env var)." >&2
  usage
  exit 2
fi

prefix="${prefix_arg:-${S3_PREFIX:-gpt-rollout-dashboard/}}"
# Strip leading slashes and ensure trailing slash for predictable key joining.
prefix="${prefix#/}"
[[ -n "$prefix" && "${prefix: -1}" != "/" ]] && prefix="${prefix}/"

region="${region_arg:-${AWS_REGION:-us-west-1}}"
profile="${profile_arg:-${AWS_PROFILE:-}}"

if [[ ! -f "$dashboard_file" ]]; then
  echo "Error: dashboard file not found: $dashboard_file" >&2
  exit 1
fi

if (( expires_seconds < 60 || expires_seconds > 604800 )); then
  echo "Error: --expires-seconds must be between 60 and 604800 (7 days)." >&2
  exit 2
fi

# ----- Helpers ---------------------------------------------------------------

aws_cli=(aws)
[[ -n "$profile" ]] && aws_cli+=(--profile "$profile")
aws_cli+=(--region "$region")

run() {
  if $dry_run; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Print on stderr so functions that capture stdout aren't polluted.
log() { printf '%s\n' "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: required command '$1' not found on PATH." >&2
    exit 1
  }
}

require_cmd aws

# Quick credential sanity check (skipped in dry-run).
if ! $dry_run; then
  if ! "${aws_cli[@]}" sts get-caller-identity --output text >/dev/null 2>&1; then
    echo "Error: AWS credentials are not available for the requested profile/region." >&2
    exit 1
  fi
fi

# ----- Upload ---------------------------------------------------------------

dashboard_basename="$(basename "$dashboard_file")"
dashboard_key="${prefix}${dashboard_basename}"

upload_one() {
  # Upload a local file to s3://bucket/key with explicit content-type and headers.
  local local_path="$1"
  local key="$2"
  local content_type="$3"

  local -a cp_args=(s3 cp "$local_path" "s3://${bucket}/${key}" --content-type "$content_type")
  if $use_no_cache; then
    cp_args+=(--cache-control "no-cache, max-age=0")
  fi
  if $make_public; then
    cp_args+=(--acl public-read)
  fi

  log "Uploading ${local_path} -> s3://${bucket}/${key}"
  run "${aws_cli[@]}" "${cp_args[@]}"
}

upload_one "$dashboard_file" "$dashboard_key" "text/html; charset=utf-8"

if $upload_samples; then
  pinot_csv="${repo_root}/examples/sample_pinot_metrics.csv"
  mysql_csv="${repo_root}/examples/sample_mysql_metrics.csv"
  [[ -f "$pinot_csv" ]] && upload_one "$pinot_csv" "${prefix}examples/$(basename "$pinot_csv")" "text/csv; charset=utf-8"
  [[ -f "$mysql_csv" ]] && upload_one "$mysql_csv" "${prefix}examples/$(basename "$mysql_csv")" "text/csv; charset=utf-8"
fi

# ----- Optional static-website configuration --------------------------------

if $configure_website; then
  log ""
  log "Configuring static website hosting on bucket '${bucket}'..."

  website_config_json=$(cat <<EOF
{
  "IndexDocument": { "Suffix": "${dashboard_basename}" },
  "ErrorDocument": { "Key": "${dashboard_basename}" }
}
EOF
)
  # aws s3api expects a file path; write the config to a temp file first.
  tmp_website_config="$(mktemp -t s3-website-config.XXXXXX.json)"
  trap 'rm -f "$tmp_website_config"' EXIT
  printf '%s\n' "$website_config_json" >"$tmp_website_config"
  run "${aws_cli[@]}" s3api put-bucket-website --bucket "$bucket" --website-configuration "file://${tmp_website_config}"

  log "Applying public-read bucket policy (only objects under '${prefix}')..."
  policy_json=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowPublicReadOfGptRolloutDashboard",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${bucket}/${prefix}*"
    }
  ]
}
EOF
)
  tmp_policy_file="$(mktemp -t s3-policy.XXXXXX.json)"
  trap 'rm -f "$tmp_website_config" "$tmp_policy_file"' EXIT
  printf '%s\n' "$policy_json" >"$tmp_policy_file"

  # The bucket may already have a policy; refuse to clobber it silently.
  if "${aws_cli[@]}" s3api get-bucket-policy --bucket "$bucket" >/dev/null 2>&1; then
    log "WARNING: Bucket '${bucket}' already has a policy. Skipping policy update."
    log "         If the existing policy does not allow public reads of '${prefix}*',"
    log "         the website URL will return 403 Forbidden."
  else
    run "${aws_cli[@]}" s3api put-bucket-policy --bucket "$bucket" --policy "file://${tmp_policy_file}" || {
      log "WARNING: Could not apply bucket policy (likely blocked by 'Block Public Access')."
      log "         The website URL may return 403 until BPA is relaxed for this bucket."
    }
  fi
fi

# ----- URL printing ---------------------------------------------------------

if [[ "$region" == "us-east-1" ]]; then
  object_url="https://${bucket}.s3.amazonaws.com/${dashboard_key}"
  website_url="http://${bucket}.s3-website-us-east-1.amazonaws.com/"
else
  object_url="https://${bucket}.s3.${region}.amazonaws.com/${dashboard_key}"
  website_url="http://${bucket}.s3-website.${region}.amazonaws.com/"
fi

presigned_url=""
if ! $dry_run; then
  presigned_url="$("${aws_cli[@]}" s3 presign "s3://${bucket}/${dashboard_key}" --expires-in "$expires_seconds")"
fi

echo
echo "=========================================================================="
echo "Deployed: ${dashboard_basename}"
echo "S3 URI:   s3://${bucket}/${dashboard_key}"
if $make_public; then
  echo "Object URL (public): ${object_url}"
else
  echo "Object URL:          ${object_url}  (requires public-read or signed access)"
fi
if $configure_website; then
  echo "Website URL:         ${website_url}"
fi
if [[ -n "$presigned_url" ]]; then
  human_hours=$(( expires_seconds / 3600 ))
  human_minutes=$(( (expires_seconds % 3600) / 60 ))
  echo "Presigned URL (valid for ${human_hours}h ${human_minutes}m):"
  echo "  ${presigned_url}"
fi
echo "=========================================================================="
