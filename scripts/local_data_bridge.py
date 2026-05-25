#!/usr/bin/env python3
"""Localhost HTTP bridge that runs the rollout dashboard's data fetches."""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Final


LOGGER: Final[logging.Logger] = logging.getLogger("local_data_bridge")
BRIDGE_VERSION: Final[str] = "1.0"
DEFAULT_PORT: Final[int] = 8765
DEFAULT_REGION: Final[str] = "us-west-2"
DEFAULT_RDS_PORT: Final[int] = 3306
RDS_CA_BUNDLE_URL: Final[str] = (
    "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
)
DEFAULT_CA_BUNDLE_PATH: Final[Path] = Path.home() / ".aws" / "rds" / "global-bundle.pem"
DEFAULT_ALLOWED_ORIGINS: Final[tuple[str, ...]] = (
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://*.netlify.app",
    "file://",
    "null",
)
SQL_FILE_NAME: Final[str] = "mysql_adoption.sql"
PINOT_SQL_FILE_NAME: Final[str] = "pinot_latency.sql"
BRAND_IDS_PLACEHOLDER: Final[str] = "__BRAND_IDS__"
WINDOW_MS_PLACEHOLDER: Final[str] = "__WINDOW_MS__"

# Forbidden tokens in any rendered Pinot SQL. Matched as whole-word, case-
# insensitive. The bridge raises before sending the query if any of these
# appear, even though the SQL template ships SELECT-only and parameters are
# strictly int-validated. Belt + braces against a future careless edit.
PINOT_FORBIDDEN_KEYWORDS: Final[tuple[str, ...]] = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "EXEC", "CALL", "MERGE", "REPLACE",
    "LOAD", "RENAME", "SET",
)
PINOT_DEFAULT_TIMEOUT_SECONDS: Final[int] = 30


class BridgeError(RuntimeError):
    """Raise when the bridge cannot satisfy a request."""

    def __init__(self, status_code: int, message: str) -> None:
        """Bind an HTTP status code to a human-readable error message."""

        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


def parse_env_value(raw_value: str) -> str:
    """Strip surrounding matching quotes from a .env value."""

    cleaned_value = raw_value.strip()
    if (
        len(cleaned_value) >= 2
        and cleaned_value[0] == cleaned_value[-1]
        and cleaned_value[0] in {"'", '"'}
    ):
        return cleaned_value[1:-1]
    return cleaned_value


def load_dotenv_file(path: Path) -> None:
    """Load env vars from a .env file without overriding what's already set."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        env_key = key.strip()
        if not env_key or env_key in os.environ:
            continue
        os.environ[env_key] = parse_env_value(raw_value)


def repo_root_from(here: Path) -> Path:
    """Compute the repo root from the bridge script's location."""

    return here.resolve().parents[1]


def split_allowed_origins(raw_value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated origin list, falling back to safe defaults."""

    if not raw_value:
        return DEFAULT_ALLOWED_ORIGINS
    parts = [item.strip() for item in raw_value.split(",")]
    cleaned = tuple(item for item in parts if item)
    return cleaned or DEFAULT_ALLOWED_ORIGINS


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> str | None:
    """Return the origin to echo on CORS responses, or None if disallowed."""

    if not origin:
        return None
    for pattern in allowed:
        if pattern == origin or fnmatch.fnmatch(origin, pattern):
            return origin
    return None


# --------------------------------------------------------------------------- #
# CA bundle handling                                                          #
# --------------------------------------------------------------------------- #


def ensure_ca_bundle(target_path: Path) -> Path:
    """Download the global RDS CA bundle on first use so TLS just works."""

    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Downloading RDS CA bundle from %s -> %s", RDS_CA_BUNDLE_URL, target_path)
    try:
        with urllib.request.urlopen(RDS_CA_BUNDLE_URL, timeout=15) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BridgeError(
            status_code=500,
            message=(
                f"Could not download RDS CA bundle from {RDS_CA_BUNDLE_URL}: {exc}. "
                f"Set BRIDGE_CA_BUNDLE to a local pem file as a workaround."
            ),
        ) from exc
    target_path.write_bytes(payload)
    return target_path


# --------------------------------------------------------------------------- #
# MySQL adoption runner                                                       #
# --------------------------------------------------------------------------- #


def strip_sql_line_comments(sql: str) -> str:
    """Drop SQL line comments so stray "%s" in comments don't confuse PyMySQL."""

    # PyMySQL counts every "%s" in the query string (including inside SQL
    # comments) against the params tuple, so a literal `-- %s example` in a
    # comment breaks parameter binding with "not enough arguments for format
    # string". We strip "-- ..." line comments before substitution.
    out_lines: list[str] = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx == -1:
            out_lines.append(line)
        else:
            out_lines.append(line[:idx].rstrip())
    return "\n".join(out_lines)


def load_sql_template(repo_root: Path) -> str:
    """Read the parameterized MySQL adoption SQL template from disk."""

    sql_path = repo_root / "scripts" / "sql" / SQL_FILE_NAME
    if not sql_path.exists():
        raise BridgeError(
            status_code=500,
            message=f"SQL file not found: {sql_path}. Reinstall the repo files.",
        )
    return strip_sql_line_comments(sql_path.read_text(encoding="utf-8"))


def parse_brand_ids(raw_value: Any) -> tuple[int, ...]:
    """Coerce a list/string brand-id input into a tuple of positive ints."""

    if raw_value is None:
        raise BridgeError(400, "brand_ids is required.")
    if isinstance(raw_value, str):
        parts = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, list):
        parts = [str(item).strip() for item in raw_value]
    else:
        raise BridgeError(400, "brand_ids must be an array or comma-separated string.")

    cleaned: list[int] = []
    for part in parts:
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise BridgeError(400, f"brand_ids contains non-integer value: {part!r}") from exc
        if value <= 0:
            raise BridgeError(400, f"brand_ids must be positive integers: {value}")
        cleaned.append(value)
    if not cleaned:
        raise BridgeError(400, "brand_ids must contain at least one positive integer.")
    # Deduplicate while preserving order.
    return tuple(dict.fromkeys(cleaned))


def parse_days(raw_value: Any, *, default: int = 1) -> int:
    """Coerce a 'days' input into an int between 1 and 365."""

    if raw_value is None or raw_value == "":
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(400, f"days must be an integer (got {raw_value!r}).") from exc
    if value < 1 or value > 365:
        raise BridgeError(400, "days must be between 1 and 365.")
    return value


def build_mysql_adoption_query(
    template: str, brand_ids: tuple[int, ...], days: int
) -> tuple[str, tuple[Any, ...]]:
    """Expand the SQL template for the requested brand_ids + days window."""

    if BRAND_IDS_PLACEHOLDER not in template:
        raise BridgeError(
            status_code=500,
            message=f"SQL template missing {BRAND_IDS_PLACEHOLDER} placeholder.",
        )
    parameterized = ", ".join(["%s"] * len(brand_ids))
    rendered_sql = template.replace(BRAND_IDS_PLACEHOLDER, parameterized)
    # Ordering: %s for days appears before the IN list, so days comes first.
    params: tuple[Any, ...] = (days, *brand_ids)
    return rendered_sql, params


def normalize_mysql_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a DB row into the dashboard's MySQL CSV row shape."""

    avg_utt = row.get("avg_utterances_per_session")
    return {
        "brand_id": str(row.get("brand_id", "")).strip(),
        "brand_name": str(row.get("brand_name", "")).strip(),
        "total_sessions": int(row.get("total_sessions") or 0),
        "total_utterances": int(row.get("total_utterances") or 0),
        "avg_utterances_per_session": float(avg_utt) if avg_utt is not None else 0.0,
    }


def fetch_mysql_adoption(
    *,
    config: "BridgeConfig",
    brand_ids: tuple[int, ...],
    days: int,
    boto3_module: Any | None = None,
    pymysql_module: Any | None = None,
) -> list[dict[str, Any]]:
    """Mint an IAM token, connect over TLS, run the query, return clean rows."""

    if not config.rds_host or not config.rds_user:
        raise BridgeError(
            500,
            "RDS_HOST and RDS_USER must be set in .env before fetching MySQL data.",
        )

    boto3 = boto3_module or _import_optional("boto3")
    pymysql = pymysql_module or _import_optional("pymysql")

    ca_bundle = ensure_ca_bundle(config.ca_bundle_path)

    rds_client = boto3.client("rds", region_name=config.rds_region)
    token = rds_client.generate_db_auth_token(
        DBHostname=config.rds_host,
        Port=config.rds_port,
        DBUsername=config.rds_user,
        Region=config.rds_region,
    )

    template = load_sql_template(config.repo_root)
    sql, params = build_mysql_adoption_query(template, brand_ids, days)

    connection = pymysql.connect(
        host=config.rds_host,
        port=config.rds_port,
        user=config.rds_user,
        password=token,
        database=config.rds_database or None,
        ssl={"ca": str(ca_bundle)},
        connect_timeout=15,
        read_timeout=60,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    finally:
        connection.close()

    return [normalize_mysql_row(row) for row in rows]


def _import_optional(name: str) -> Any:
    """Import boto3/pymysql lazily with a friendly install hint on failure."""

    try:
        return __import__(name)
    except ImportError as exc:
        raise BridgeError(
            500,
            f"Required dependency '{name}' is not installed. "
            "Run: pip install -r requirements.txt",
        ) from exc


# --------------------------------------------------------------------------- #
# CloudWatch (lambda) runner                                                  #
# --------------------------------------------------------------------------- #


CW_COLLECTOR_TIMEOUT_SECONDS: Final[int] = 300


def _load_review_module(repo_root: Path) -> Any:
    """Load scripts/run_cloudwatch_review.py as a module without polluting sys.path."""

    import importlib.util

    module_path = repo_root / "scripts" / "run_cloudwatch_review.py"
    if not module_path.exists():
        raise BridgeError(500, f"Review module not found: {module_path}.")
    spec = importlib.util.spec_from_file_location("run_cloudwatch_review", module_path)
    if spec is None or spec.loader is None:
        raise BridgeError(500, "Unable to load run_cloudwatch_review.py.")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_cloudwatch_review", module)
    spec.loader.exec_module(module)
    return module


def parse_lambda_names(raw_value: Any, *, default_for_env: Callable[[str], tuple[str, ...]],
                       env_name: str) -> tuple[str, ...]:
    """Coerce list/string input into a non-empty tuple of Lambda function names."""

    if raw_value is None or raw_value == "":
        return tuple(default_for_env(env_name))
    if isinstance(raw_value, str):
        parts = [item.strip() for item in raw_value.replace("\n", ",").split(",")]
    elif isinstance(raw_value, list):
        parts = [str(item).strip() for item in raw_value]
    else:
        raise BridgeError(400, "lambda_names must be an array or comma-separated string.")
    cleaned = tuple(item for item in parts if item)
    if not cleaned:
        return tuple(default_for_env(env_name))
    return cleaned


def parse_ignore_messages(raw_value: Any) -> tuple[str, ...]:
    """Accept a list/comma-string of extra ignore patterns to subtract from logs."""

    if not raw_value:
        return ()
    if isinstance(raw_value, str):
        parts = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, list):
        parts = [str(item).strip() for item in raw_value]
    else:
        raise BridgeError(400, "ignore_messages must be a list or comma-separated string.")
    return tuple(item for item in parts if item)


def _format_optional_int(value: Any) -> str:
    """Render an int for CSV serialization, blank when missing."""

    return "" if value is None else str(int(value))


def _format_optional_ms(value: Any) -> str:
    """Render a millisecond float with two decimals, blank when missing."""

    return "" if value is None else f"{float(value):.2f}"


def _row_from_summary(review_module: Any, lambda_name: str, summary: Any,
                      top_messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a LambdaSummary + top_messages slice into a dashboard row."""

    return {
        "lambda_name": lambda_name,
        "invocations": _format_optional_int(summary.invocations),
        "lambda_errors": _format_optional_int(summary.lambda_errors),
        "throttles": _format_optional_int(summary.throttles),
        "avg_duration_ms": _format_optional_ms(summary.avg_duration_ms),
        "p95_duration_ms": _format_optional_ms(summary.p95_duration_ms),
        "p99_duration_ms": _format_optional_ms(summary.p99_duration_ms),
        "max_duration_ms": _format_optional_ms(summary.max_duration_ms),
        "cold_starts": _format_optional_int(summary.cold_starts),
        "avg_init_duration_ms": _format_optional_ms(summary.avg_init_duration_ms),
        "warning_count": _format_optional_int(summary.warning_count),
        "error_count": _format_optional_int(summary.error_count),
        "top_errors": review_module.encode_top_messages_for_csv(
            top_messages, lambda_name, "ERROR"
        ),
        "top_warnings": review_module.encode_top_messages_for_csv(
            top_messages, lambda_name, "WARNING"
        ),
    }


def fetch_lambda_cloudwatch(
    *,
    config: "BridgeConfig",
    env_name: str,
    region: str,
    days: int,
    lambda_names: tuple[str, ...],
    ignored_messages: tuple[str, ...],
    use_default_ignores: bool,
    subprocess_runner: Callable[..., Any] | None = None,
    review_module: Any | None = None,
) -> dict[str, Any]:
    """Run the cloudwatch collector + parsers and return CSV-shaped lambda rows."""

    review = review_module or _load_review_module(config.repo_root)
    runner = subprocess_runner or _run_subprocess

    # Reuse the same rolling-window semantics the script exposes interactively
    # (daily, weekly, ...). We accept just "days" here since the dashboard only
    # asks for an integer; map it to the closest preset, falling back to a
    # custom window when none matches.
    end_dt = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    start_dt = end_dt - _dt.timedelta(days=days)
    start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    collector_path = config.repo_root / "scripts" / "cloudwatch_review_collect.sh"
    if not collector_path.exists():
        raise BridgeError(500, f"Collector script missing: {collector_path}.")

    command = [
        "bash",
        str(collector_path),
        "--env", env_name,
        "--region", region,
        "--start", start_time,
        "--end", end_time,
    ]
    for name in lambda_names:
        command.extend(["--lambda", name])

    LOGGER.info(
        "Running cloudwatch collector for env=%s region=%s days=%d lambdas=%s",
        env_name, region, days, ",".join(lambda_names),
    )
    completed = runner(command, cwd=str(config.repo_root), timeout=CW_COLLECTOR_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "")[-1000:]
        raise BridgeError(
            502,
            "cloudwatch_review_collect.sh failed: "
            f"rc={completed.returncode}. Tail: {stderr_tail.strip() or '(empty)'}",
        )

    run_dir = config.repo_root / "artifacts" / "cloudwatch-review" / (
        f"{env_name}-"
        f"{review.sanitize_for_path(start_time)}_"
        f"{review.sanitize_for_path(end_time)}"
    )
    if not run_dir.exists():
        raise BridgeError(
            500,
            f"Expected artifact directory was not produced: {run_dir}.",
        )

    ignored = review.load_ignored_messages(
        cli_patterns=ignored_messages,
        use_defaults=use_default_ignores,
    )
    _meta_scalars, _meta_sections, summaries, top_messages, _filtered = (
        review.load_lambda_summaries(run_dir, ignored_messages=ignored)
    )

    rows = [
        _row_from_summary(review, lambda_name, summaries[lambda_name], top_messages)
        for lambda_name in lambda_names
        if lambda_name in summaries
    ]
    return {
        "rows": rows,
        "run_dir": str(run_dir),
        "start_time": start_time,
        "end_time": end_time,
        "ignored_messages": list(ignored),
    }


def _run_subprocess(command: list[str], *, cwd: str, timeout: int) -> Any:
    """Wrap subprocess.run so tests can inject a fake without monkeypatching."""

    import subprocess

    return subprocess.run(  # noqa: S603 (collector path is controlled by us)
        command,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Pinot (latency) runner                                                      #
# --------------------------------------------------------------------------- #


_WORD_RE: Final[Any] = __import__("re").compile(r"[A-Za-z_][A-Za-z_0-9]*")


def assert_select_only(sql: str) -> None:
    """Raise BridgeError if the SQL isn't a single SELECT statement."""

    stripped = strip_sql_line_comments(sql).strip()
    if not stripped:
        raise BridgeError(500, "Empty SQL after comment stripping.")

    # Allow at most one trailing semicolon (Pinot ignores it, MySQL accepts one).
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if ";" in stripped:
        raise BridgeError(500, "Multiple SQL statements are not allowed.")

    # Must begin with the SELECT keyword.
    head_match = _WORD_RE.match(stripped)
    if not head_match or head_match.group(0).upper() != "SELECT":
        raise BridgeError(500, "Only SELECT statements may be sent to Pinot.")

    # Inspect every word-token for forbidden DDL/DML keywords.
    for word in _WORD_RE.findall(stripped):
        if word.upper() in PINOT_FORBIDDEN_KEYWORDS:
            raise BridgeError(
                500,
                f"Forbidden SQL keyword '{word.upper()}' in Pinot query.",
            )


def build_pinot_latency_query(
    template: str, brand_ids: tuple[int, ...], window_ms: int
) -> str:
    """Substitute window + brand-id placeholders, then enforce SELECT-only."""

    if WINDOW_MS_PLACEHOLDER not in template:
        raise BridgeError(
            500, f"Pinot SQL template missing {WINDOW_MS_PLACEHOLDER} placeholder.",
        )
    if BRAND_IDS_PLACEHOLDER not in template:
        raise BridgeError(
            500, f"Pinot SQL template missing {BRAND_IDS_PLACEHOLDER} placeholder.",
        )
    # brand_ids and window_ms are pre-validated to ints by parse_*, so direct
    # string substitution is safe. assert_select_only() runs as a final guard.
    rendered = template.replace(WINDOW_MS_PLACEHOLDER, str(int(window_ms)))
    rendered = rendered.replace(
        BRAND_IDS_PLACEHOLDER,
        ", ".join(str(int(b)) for b in brand_ids),
    )
    assert_select_only(rendered)
    return rendered


def load_pinot_template(repo_root: Path) -> str:
    """Read the Pinot SQL template from disk and strip line comments."""

    sql_path = repo_root / "scripts" / "sql" / PINOT_SQL_FILE_NAME
    if not sql_path.exists():
        raise BridgeError(500, f"Pinot SQL file not found: {sql_path}.")
    return strip_sql_line_comments(sql_path.read_text(encoding="utf-8"))


def _normalize_pinot_row(column_names: list[str], values: list[Any]) -> dict[str, Any]:
    """Zip a Pinot result row into the dashboard's 11-column Pinot row shape."""

    row: dict[str, Any] = {}
    for name, value in zip(column_names, values):
        row[name] = "" if value is None else value
    # Coerce every numeric to a stable string representation so the dashboard's
    # validateColumns + parseCsv-shaped objects keep working unchanged.
    return {
        "brand_id": str(row.get("brand_id", "")).strip(),
        "first_total_requests": _to_int_str(row.get("first_total_requests")),
        "first_avg_ttfb_ms": _to_float_str(row.get("first_avg_ttfb_ms")),
        "first_p50_ttfb_ms": _to_float_str(row.get("first_p50_ttfb_ms")),
        "first_p95_ttfb_ms": _to_float_str(row.get("first_p95_ttfb_ms")),
        "first_p99_ttfb_ms": _to_float_str(row.get("first_p99_ttfb_ms")),
        "followup_total_requests": _to_int_str(row.get("followup_total_requests")),
        "followup_avg_ttfb_ms": _to_float_str(row.get("followup_avg_ttfb_ms")),
        "followup_p50_ttfb_ms": _to_float_str(row.get("followup_p50_ttfb_ms")),
        "followup_p95_ttfb_ms": _to_float_str(row.get("followup_p95_ttfb_ms")),
        "followup_p99_ttfb_ms": _to_float_str(row.get("followup_p99_ttfb_ms")),
    }


def _to_int_str(value: Any) -> str:
    """Render an int-y value as a string, blank when missing."""

    if value is None or value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def _to_float_str(value: Any) -> str:
    """Render a float-y value with two decimals, blank when missing."""

    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def fetch_pinot_latency(
    *,
    config: "BridgeConfig",
    brand_ids: tuple[int, ...],
    days: int,
    pinot_auth_token: str,
    http_runner: Callable[..., Any] | None = None,
    template_loader: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    """Render the Pinot SQL, POST it to /sql, and return CSV-shaped rows."""

    base_url = (config.pinot_base_url or "").strip().rstrip("/")
    if not base_url:
        raise BridgeError(
            500,
            "PINOT_BASE_URL must be set in .env (e.g. https://pinot.<tenant>.cp.s7e.startree.cloud).",
        )
    if not pinot_auth_token:
        raise BridgeError(
            401,
            "Pinot auth token is required. Paste your Bearer token into the "
            "dashboard's 'Pinot token' field or set PINOT_AUTH_TOKEN in .env.",
        )

    loader = template_loader or load_pinot_template
    template = loader(config.repo_root)
    # Rolling N-day window in milliseconds; we keep the units in Pinot's native
    # epoch-ms because the underlying column is `first_chunk_timestamp` (ms).
    window_ms = int(days) * 86_400_000
    sql = build_pinot_latency_query(template, brand_ids, window_ms)

    body = json.dumps({"sql": sql, "trace": False, "queryOptions": ""}).encode("utf-8")
    runner = http_runner or _post_pinot_sql

    LOGGER.info(
        "Fetching Pinot latency: window_ms=%d brand_ids=%s",
        window_ms, ",".join(str(b) for b in brand_ids),
    )
    response = runner(
        url=f"{base_url}/sql",
        body=body,
        bearer_token=pinot_auth_token,
        timeout=PINOT_DEFAULT_TIMEOUT_SECONDS,
    )

    if response.status == 401 or response.status == 403:
        raise BridgeError(
            401,
            "Pinot rejected the auth token (HTTP "
            f"{response.status}). Mint a new Bearer token and try again.",
        )
    if response.status >= 500:
        raise BridgeError(
            502,
            f"Pinot returned {response.status}. Tail: {response.body_text[:300]!r}",
        )
    if response.status >= 400:
        raise BridgeError(
            response.status,
            f"Pinot rejected the request ({response.status}): {response.body_text[:300]}",
        )

    try:
        payload = json.loads(response.body_text)
    except json.JSONDecodeError as exc:
        raise BridgeError(502, f"Pinot returned non-JSON body: {exc}") from exc

    exceptions = payload.get("exceptions") or []
    if exceptions:
        first = exceptions[0]
        message = first.get("message") if isinstance(first, dict) else str(first)
        raise BridgeError(502, f"Pinot query failed: {message}")

    result_table = payload.get("resultTable") or {}
    schema = result_table.get("dataSchema") or {}
    column_names = list(schema.get("columnNames") or [])
    rows_raw = result_table.get("rows") or []
    rows = [_normalize_pinot_row(column_names, row) for row in rows_raw]

    return {
        "rows": rows,
        "window_ms": window_ms,
        "num_docs_scanned": payload.get("numDocsScanned"),
        "time_used_ms": payload.get("timeUsedMs"),
    }


class _PinotHttpResponse:
    """Lightweight HTTP response container for the Pinot runner."""

    def __init__(self, status: int, body_text: str) -> None:
        """Bind the HTTP status + decoded body."""

        self.status = status
        self.body_text = body_text


def _post_pinot_sql(*, url: str, body: bytes, bearer_token: str, timeout: int) -> _PinotHttpResponse:
    """Stdlib HTTP POST so tests can inject a fake without a real socket."""

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            return _PinotHttpResponse(resp.status, raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return _PinotHttpResponse(exc.code, raw.decode("utf-8", errors="replace"))
    except urllib.error.URLError as exc:
        raise BridgeError(
            502,
            f"Could not reach Pinot at {url}: {exc.reason}. "
            "If you're on Cisco VPN, confirm the connection is active.",
        ) from exc


# --------------------------------------------------------------------------- #
# Bridge configuration object                                                 #
# --------------------------------------------------------------------------- #


class BridgeConfig:
    """Capture the runtime config used by every request handler."""

    def __init__(
        self,
        *,
        repo_root: Path,
        port: int,
        require_auth: bool,
        bridge_token: str,
        allowed_origins: tuple[str, ...],
        rds_host: str,
        rds_port: int,
        rds_user: str,
        rds_region: str,
        rds_database: str,
        ca_bundle_path: Path,
        pinot_base_url: str = "",
        pinot_auth_token: str = "",
    ) -> None:
        """Bind all runtime knobs onto the config object."""

        self.repo_root = repo_root
        self.port = port
        self.require_auth = require_auth
        self.bridge_token = bridge_token
        self.allowed_origins = allowed_origins
        self.rds_host = rds_host
        self.rds_port = rds_port
        self.rds_user = rds_user
        self.rds_region = rds_region
        self.rds_database = rds_database
        self.ca_bundle_path = ca_bundle_path
        # Pinot is optional; the endpoint still rejects calls without a token,
        # but the bridge can boot fine even if Pinot env vars are unset.
        self.pinot_base_url = pinot_base_url
        self.pinot_auth_token = pinot_auth_token


def build_config_from_env(args: argparse.Namespace) -> BridgeConfig:
    """Combine .env values + CLI args into a single BridgeConfig instance."""

    repo_root = repo_root_from(Path(__file__))
    load_dotenv_file(repo_root / ".env")

    port = int(args.port or os.getenv("BRIDGE_PORT", str(DEFAULT_PORT)))
    require_auth = not args.no_auth
    bridge_token = secrets.token_hex(16) if require_auth else ""

    allowed_origins = split_allowed_origins(os.getenv("BRIDGE_ALLOWED_ORIGINS"))
    ca_bundle_env = os.getenv("BRIDGE_CA_BUNDLE")
    ca_bundle_path = Path(ca_bundle_env).expanduser() if ca_bundle_env else DEFAULT_CA_BUNDLE_PATH

    return BridgeConfig(
        repo_root=repo_root,
        port=port,
        require_auth=require_auth,
        bridge_token=bridge_token,
        allowed_origins=allowed_origins,
        rds_host=os.getenv("RDS_HOST", "").strip(),
        rds_port=int(os.getenv("RDS_PORT", str(DEFAULT_RDS_PORT))),
        rds_user=os.getenv("RDS_USER", "").strip(),
        rds_region=os.getenv("RDS_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION,
        rds_database=os.getenv("RDS_DATABASE", "").strip(),
        ca_bundle_path=ca_bundle_path,
        pinot_base_url=os.getenv("PINOT_BASE_URL", "").strip(),
        pinot_auth_token=os.getenv("PINOT_AUTH_TOKEN", "").strip(),
    )


# --------------------------------------------------------------------------- #
# HTTP request handler                                                        #
# --------------------------------------------------------------------------- #


def make_handler(
    config: BridgeConfig,
    *,
    mysql_runner: Callable[..., list[dict[str, Any]]] = fetch_mysql_adoption,
    lambda_runner: Callable[..., dict[str, Any]] = fetch_lambda_cloudwatch,
    pinot_runner: Callable[..., dict[str, Any]] = fetch_pinot_latency,
) -> type[BaseHTTPRequestHandler]:
    """Bake config + injectable runners into a request-handler class."""

    class _Handler(BaseHTTPRequestHandler):
        # Quiet down the default access log; we have our own.
        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("%s - %s", self.address_string(), format % args)

        # ---- response helpers --------------------------------------------- #

        def _send_cors(self) -> None:
            origin = self.headers.get("Origin")
            echo = origin_allowed(origin, config.allowed_origins)
            if echo:
                self.send_header("Access-Control-Allow-Origin", echo)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token"
            )
            self.send_header("Access-Control-Max-Age", "600")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BridgeError(400, f"Invalid JSON body: {exc}") from exc
            if not isinstance(value, dict):
                raise BridgeError(400, "Request body must be a JSON object.")
            return value

        def _check_auth(self) -> None:
            if not config.require_auth:
                return
            provided = self.headers.get("X-Bridge-Token", "").strip()
            if not provided or not secrets.compare_digest(provided, config.bridge_token):
                raise BridgeError(401, "Missing or invalid X-Bridge-Token header.")

        # ---- HTTP verbs --------------------------------------------------- #

        def do_OPTIONS(self) -> None:  # noqa: N802 (CGI-style spelling required)
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            try:
                if self.path == "/healthz":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "version": BRIDGE_VERSION,
                            "datasets": [
                                "mysql.adoption",
                                "lambda.cloudwatch",
                                "pinot.latency",
                            ],
                            "auth_required": config.require_auth,
                        },
                    )
                    return
                raise BridgeError(404, f"No GET handler for {self.path}")
            except BridgeError as err:
                self._send_json(err.status_code, {"error": err.message})

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._check_auth()
                if self.path == "/query/mysql/adoption":
                    self._handle_mysql_adoption()
                elif self.path == "/query/pinot/latency":
                    self._handle_pinot_latency()
                elif self.path == "/query/lambda/cloudwatch":
                    self._handle_lambda_cloudwatch()
                else:
                    raise BridgeError(404, f"No POST handler for {self.path}")
            except BridgeError as err:
                LOGGER.warning("[%s] %s -> %d %s", "POST", self.path, err.status_code, err.message)
                self._send_json(err.status_code, {"error": err.message})
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Unhandled error servicing %s", self.path)
                self._send_json(500, {"error": f"Internal bridge error: {exc}"})

        # ---- dataset implementations -------------------------------------- #

        def _handle_mysql_adoption(self) -> None:
            body = self._read_json_body()
            brand_ids = parse_brand_ids(body.get("brand_ids"))
            days = parse_days(body.get("days"))
            LOGGER.info(
                "Fetching MySQL adoption: days=%d brand_ids=%s",
                days, ",".join(str(b) for b in brand_ids),
            )
            rows = mysql_runner(config=config, brand_ids=brand_ids, days=days)
            self._send_json(
                200,
                {
                    "rows": rows,
                    "row_count": len(rows),
                    "fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "days": days,
                    "brand_ids": list(brand_ids),
                },
            )

        def _handle_pinot_latency(self) -> None:
            body = self._read_json_body()
            brand_ids = parse_brand_ids(body.get("brand_ids"))
            days = parse_days(body.get("days"))
            # Body-supplied token takes precedence over the env-var default so
            # the user can paste a fresh JWT into the dashboard without having
            # to edit .env every 24h.
            token = str(body.get("pinot_auth_token") or "").strip() or config.pinot_auth_token

            LOGGER.info(
                "Fetching Pinot latency: days=%d brand_ids=%s",
                days, ",".join(str(b) for b in brand_ids),
            )
            result = pinot_runner(
                config=config,
                brand_ids=brand_ids,
                days=days,
                pinot_auth_token=token,
            )
            self._send_json(
                200,
                {
                    "rows": result["rows"],
                    "row_count": len(result["rows"]),
                    "fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "days": days,
                    "window_ms": result.get("window_ms"),
                    "brand_ids": list(brand_ids),
                    "num_docs_scanned": result.get("num_docs_scanned"),
                    "time_used_ms": result.get("time_used_ms"),
                },
            )

        def _handle_lambda_cloudwatch(self) -> None:
            body = self._read_json_body()
            env_name = str(body.get("env") or "prod").strip() or "prod"
            region = str(body.get("region") or config.rds_region or DEFAULT_REGION).strip()
            days = parse_days(body.get("days"))
            # Lambda names: fall back to the canonical default set for the env.
            review_for_defaults = _load_review_module(config.repo_root)
            lambda_names = parse_lambda_names(
                body.get("lambda_names"),
                default_for_env=review_for_defaults.default_lambda_names,
                env_name=env_name,
            )
            ignored_messages = parse_ignore_messages(body.get("ignore_messages"))
            use_default_ignores = bool(body.get("use_default_ignores", True))

            LOGGER.info(
                "Fetching Lambda CloudWatch: env=%s region=%s days=%d lambdas=%s",
                env_name, region, days, ",".join(lambda_names),
            )
            result = lambda_runner(
                config=config,
                env_name=env_name,
                region=region,
                days=days,
                lambda_names=lambda_names,
                ignored_messages=ignored_messages,
                use_default_ignores=use_default_ignores,
            )
            self._send_json(
                200,
                {
                    "rows": result["rows"],
                    "row_count": len(result["rows"]),
                    "fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "env": env_name,
                    "region": region,
                    "days": days,
                    "lambda_names": list(lambda_names),
                    "start_time": result.get("start_time"),
                    "end_time": result.get("end_time"),
                    "ignored_messages": result.get("ignored_messages", []),
                },
            )

    return _Handler


# --------------------------------------------------------------------------- #
# Server bootstrap                                                            #
# --------------------------------------------------------------------------- #


def find_free_port(start_port: int, host: str = "127.0.0.1") -> int:
    """Pick the configured port if free, otherwise raise so the user can act."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, start_port))
    except OSError as exc:
        raise BridgeError(
            500,
            f"Port {start_port} is already in use on 127.0.0.1. "
            "Pass --port <other> or stop the other process.",
        ) from exc
    finally:
        sock.close()
    return start_port


def serve(config: BridgeConfig) -> None:
    """Boot the threading HTTP server and block until SIGINT."""

    find_free_port(config.port)
    handler_cls = make_handler(config)
    server = ThreadingHTTPServer(("127.0.0.1", config.port), handler_cls)

    LOGGER.info("Local data bridge listening on http://127.0.0.1:%d", config.port)
    if config.require_auth:
        LOGGER.info("Bridge token: %s", config.bridge_token)
        LOGGER.info("  Paste it into the dashboard's 'Bridge token' field.")
    else:
        LOGGER.warning("Auth disabled (--no-auth). Do not use over untrusted networks.")
    LOGGER.info("Datasets: mysql.adoption, lambda.cloudwatch, pinot.latency")

    stop_event = threading.Event()

    def _shutdown_signal(*_: Any) -> None:
        """Translate SIGINT/SIGTERM into a clean server shutdown."""

        LOGGER.info("Shutdown signal received; closing bridge...")
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown_signal)
    signal.signal(signal.SIGTERM, _shutdown_signal)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        LOGGER.info("Bridge stopped.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse bridge CLI arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Localhost HTTP bridge for the GPT rollout dashboard. Mints RDS IAM "
            "tokens via boto3 and serves the MySQL adoption query at "
            "/query/mysql/adoption. Browser-only; never binds outside 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Override BRIDGE_PORT (default 8765)."
    )
    parser.add_argument(
        "--no-auth", action="store_true",
        help="Disable bridge-token auth for local dev. Don't use this on shared machines.",
    )
    parser.add_argument(
        "--print-token-only", action="store_true",
        help="Print only the freshly generated bridge token and exit (for eval).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable DEBUG logging."
    )
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    """Initialize structured stderr logging for the bridge."""

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python3 scripts/local_data_bridge.py`."""

    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        config = build_config_from_env(args)
    except BridgeError as err:
        LOGGER.error("%s", err.message)
        return 2

    if args.print_token_only:
        if not config.require_auth:
            print("")
            return 0
        print(config.bridge_token)
        return 0

    try:
        serve(config)
    except BridgeError as err:
        LOGGER.error("%s", err.message)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
