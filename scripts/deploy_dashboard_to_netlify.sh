#!/usr/bin/env bash

# Deploy the GPT rollout dashboard HTML to Netlify and print the public URL.
#
# The dashboard is a single self-contained HTML file with no build step, so we
# stage it (renamed to index.html so it serves at the site root) plus optional
# sample CSVs into a temp directory, then push that directory with the Netlify
# CLI. Auth works via the NETLIFY_AUTH_TOKEN env var (preferred for scripted /
# CI use) or the interactive `netlify login` token if you've run that locally.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy_dashboard_to_netlify.sh [options]

Options:
  --site <id-or-name>     Existing Netlify site to deploy into. Without this,
                          the CLI will prompt to create a new site (or you can
                          use --create-site to do it non-interactively).
  --create-site <name>    Create a new Netlify site with the given subdomain
                          (e.g. "zen-gpt-rollout" -> zen-gpt-rollout.netlify.app)
                          and deploy into it.
  --draft                 Deploy to a temporary draft URL instead of production.
                          Useful for sharing a one-off snapshot.
  --samples               Also publish the bundled example CSVs under /examples/.
  --dashboard-file <path> Override the HTML file to publish.
                          Default: dashboards/gpt_rollout_dashboard.html
  --keep-publish-dir      Don't delete the staged publish directory afterwards
                          (so you can drag-and-drop it manually if you prefer).
  --dry-run               Print the actions without executing the Netlify CLI.
  --help                  Show this help text.

Environment variables:
  NETLIFY_AUTH_TOKEN      Personal access token from https://app.netlify.com/user/applications.
                          Required for non-interactive (scripted) auth.
  NETLIFY_SITE_ID         Default --site value if the flag is omitted.

Examples:
  # First deploy: create a new site and push to production.
  NETLIFY_AUTH_TOKEN=xxx ./scripts/deploy_dashboard_to_netlify.sh \
      --create-site zen-gpt-rollout

  # Redeploy to an existing site (id from `netlify sites:list` or the dashboard URL).
  NETLIFY_AUTH_TOKEN=xxx ./scripts/deploy_dashboard_to_netlify.sh \
      --site 12345678-aaaa-bbbb-cccc-1234567890ab

  # Share a one-off draft URL without overwriting production.
  ./scripts/deploy_dashboard_to_netlify.sh --site <site-id> --draft --samples

No-script alternative:
  Visit https://app.netlify.com/drop and drop dashboards/gpt_rollout_dashboard.html
  onto the page. Netlify will host it instantly at a randomly-generated URL.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

load_env_file() {
  # Reuse the .env loader pattern from the rest of the repo.
  local env_path="${repo_root}/.env"
  [[ -f "$env_path" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$env_path"
  set +a
}

# ----- Argument parsing -----------------------------------------------------

site_id="${NETLIFY_SITE_ID:-}"
new_site_name=""
deploy_prod=true
upload_samples=false
dashboard_file="${repo_root}/dashboards/gpt_rollout_dashboard.html"
keep_publish_dir=false
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site)               site_id="$2"; shift 2 ;;
    --create-site)        new_site_name="$2"; shift 2 ;;
    --draft)              deploy_prod=false; shift ;;
    --samples)            upload_samples=true; shift ;;
    --dashboard-file)     dashboard_file="$2"; shift 2 ;;
    --keep-publish-dir)   keep_publish_dir=true; shift ;;
    --dry-run)            dry_run=true; shift ;;
    --help|-h)            usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

load_env_file

if [[ ! -f "$dashboard_file" ]]; then
  echo "Error: dashboard file not found: $dashboard_file" >&2
  exit 1
fi

if [[ -n "$site_id" && -n "$new_site_name" ]]; then
  echo "Error: pass either --site or --create-site, not both." >&2
  exit 2
fi

# ----- Tooling -------------------------------------------------------------

log() { printf '%s\n' "$*" >&2; }

run() {
  if $dry_run; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: required command '$1' not found on PATH." >&2
    exit 1
  }
}

# We prefer a locally-installed `netlify` binary but fall back to `npx -y netlify-cli`
# so the user doesn't need a global install. Both invocations accept the same args.
netlify_cli=()
if command -v netlify >/dev/null 2>&1; then
  netlify_cli=(netlify)
else
  require_cmd npx
  netlify_cli=(npx -y netlify-cli@latest)
  log "Note: using 'npx netlify-cli' on-demand (first run may take ~30s)."
fi

# Warn early if there's no auth method available; the CLI itself will error
# later, but we can short-circuit with a friendlier message.
if [[ -z "${NETLIFY_AUTH_TOKEN:-}" ]] && ! $dry_run; then
  if [[ ! -f "${HOME}/.netlify/config.json" ]] && [[ ! -f "${HOME}/Library/Preferences/netlify/config.json" ]]; then
    log "Warning: NETLIFY_AUTH_TOKEN is not set and no local Netlify login was found."
    log "         Run 'netlify login' interactively or export NETLIFY_AUTH_TOKEN."
    log "         Get a token at https://app.netlify.com/user/applications#personal-access-tokens"
  fi
fi

# ----- Stage publish directory ---------------------------------------------

publish_dir="$(mktemp -d -t gpt-rollout-netlify.XXXXXX)"
if ! $keep_publish_dir; then
  trap 'rm -rf "$publish_dir"' EXIT
fi

# Copy the dashboard as index.html so it renders at the site root.
cp "$dashboard_file" "${publish_dir}/index.html"
log "Staged $(basename "$dashboard_file") -> ${publish_dir}/index.html"

if $upload_samples; then
  mkdir -p "${publish_dir}/examples"
  for csv in \
    "${repo_root}/examples/sample_pinot_metrics.csv" \
    "${repo_root}/examples/sample_mysql_metrics.csv"; do
    if [[ -f "$csv" ]]; then
      cp "$csv" "${publish_dir}/examples/"
      log "Staged $(basename "$csv") -> ${publish_dir}/examples/$(basename "$csv")"
    fi
  done
fi

# ----- Optionally create the site first ------------------------------------

if [[ -n "$new_site_name" ]]; then
  log ""
  log "Creating Netlify site '${new_site_name}'..."
  # `sites:create` exits non-zero if the name is taken; surface that verbatim.
  if $dry_run; then
    run "${netlify_cli[@]}" sites:create --name "$new_site_name"
    site_id="<dry-run-site-id>"
  else
    create_json="$("${netlify_cli[@]}" sites:create --name "$new_site_name" --json)"
    site_id="$(printf '%s' "$create_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    log "Created site id: ${site_id}"
  fi
fi

# ----- Deploy ---------------------------------------------------------------

deploy_args=(deploy --dir="$publish_dir" --json)
$deploy_prod && deploy_args+=(--prod)
[[ -n "$site_id" ]] && deploy_args+=(--site "$site_id")
# A concise message helps when scanning the Netlify deploys list later.
deploy_args+=(--message "gpt-rollout dashboard ($(date -u +%FT%TZ))")

log ""
log "Deploying to Netlify..."
if $dry_run; then
  run "${netlify_cli[@]}" "${deploy_args[@]}"
  echo
  echo "=========================================================================="
  echo "DRY-RUN complete. Staged files in: ${publish_dir}"
  echo "=========================================================================="
  exit 0
fi

deploy_output="$("${netlify_cli[@]}" "${deploy_args[@]}")"

# The CLI prints a JSON object containing deploy_url + (when --prod) url.
# Parse it with python3 for safety; jq isn't guaranteed to be installed.
parsed="$(printf '%s' "$deploy_output" | python3 - <<'PY'
import json, sys
data = json.loads(sys.stdin.read())
print(data.get("deploy_url", ""))
print(data.get("url", ""))
print(data.get("logs", ""))
PY
)"
deploy_url="$(printf '%s' "$parsed" | sed -n '1p')"
prod_url="$(printf '%s' "$parsed" | sed -n '2p')"
logs_url="$(printf '%s' "$parsed" | sed -n '3p')"

echo
echo "=========================================================================="
echo "Deployed: $(basename "$dashboard_file")  (staged as index.html)"
if $deploy_prod; then
  echo "Production URL:  ${prod_url:-<not returned>}"
  echo "Deploy snapshot: ${deploy_url:-<not returned>}"
else
  echo "Draft URL:       ${deploy_url:-<not returned>}"
fi
if [[ -n "$logs_url" ]]; then
  echo "Build logs:      ${logs_url}"
fi
if $upload_samples; then
  echo "Sample CSVs at:  ${prod_url:-${deploy_url}}/examples/"
fi
if $keep_publish_dir; then
  echo "Publish dir:     ${publish_dir}  (kept; remove manually when done)"
fi
echo "=========================================================================="
