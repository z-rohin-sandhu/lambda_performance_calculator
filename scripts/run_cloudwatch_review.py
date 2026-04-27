#!/usr/bin/env python3
"""Run the CloudWatch review automation flow."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final


LOGGER: Final[logging.Logger] = logging.getLogger("cloudwatch_review_runner")
REPORT_FILENAME_TEMPLATE: Final[str] = "{env}_{execution_date}_websocket_reports.md"
SECTION_SEPARATOR: Final[str] = "------------------------------------------------------------"
LAMBDA_PREFIX: Final[str] = "Lambda:"


class AutomationError(RuntimeError):
    """Raise when the CloudWatch review automation cannot proceed."""


@dataclass(frozen=True)
class ReviewInputs:
    """Store the interactive inputs for the review run."""

    env_name: str
    start_time: str
    end_time: str
    region: str
    lambda_names: tuple[str, ...]


@dataclass(frozen=True)
class LambdaSummary:
    """Store derived summary data for a Lambda."""

    name: str
    invocations: int | None = None
    lambda_errors: int | None = None
    throttles: int | None = None
    avg_duration_ms: float | None = None
    p95_duration_ms: float | None = None
    p99_duration_ms: float | None = None
    max_duration_ms: float | None = None
    cold_starts: int | None = None
    avg_init_duration_ms: float | None = None
    warning_count: int = 0
    error_count: int = 0
    max_concurrency: float | None = None
    in_vpc: bool = False
    state: str | None = None
    update_status: str | None = None


def default_end_time() -> str:
    """Return the default UTC end time."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_start_time() -> str:
    """Return the default UTC start time."""

    return (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a datetime object."""

    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AutomationError(
            f"Invalid timestamp '{value}'. Use UTC ISO 8601 like 2026-04-27T23:59:59Z."
        ) from exc


def sanitize_for_path(value: str) -> str:
    """Sanitize a value so it matches the shell collector path logic."""

    return value.replace(":", "-").replace(" ", "_").replace("/", "-")


def parse_env_value(raw_value: str) -> str:
    """Parse a single .env value."""

    cleaned_value = raw_value.strip()
    if len(cleaned_value) >= 2 and cleaned_value[0] == cleaned_value[-1] and cleaned_value[0] in {"'", '"'}:
        return cleaned_value[1:-1]
    return cleaned_value


def load_dotenv_file(path: Path) -> None:
    """Load environment variables from a .env file without overriding existing values."""

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue

        key, raw_value = stripped_line.split("=", 1)
        env_key = key.strip()
        if not env_key or env_key in os.environ:
            continue

        os.environ[env_key] = parse_env_value(raw_value)


def prompt_with_default(prompt_text: str, default_value: str) -> str:
    """Prompt the user and apply a default value when blank."""

    raw_value = input(f"{prompt_text} [{default_value}]: ").strip()
    return raw_value or default_value


def default_lambda_names(env_name: str) -> tuple[str, ...]:
    """Return the collector's default Lambda list for an environment."""

    return (
        f"zen-{env_name}-sqs-message-consumer",
        f"zen-{env_name}-ws-to-sqs-producer",
        f"zen_{env_name}_authorizer_service"
    )


def prompt_lambda_names(env_name: str) -> tuple[str, ...]:
    """Collect one or more Lambda names from the terminal."""

    print("\nEnter Lambda names one per line.")
    print("Press Enter on an empty line to finish.")
    print("If you leave the list empty, the collector defaults will be used.\n")

    lambda_names: list[str] = []
    while True:
        raw_value = input("Lambda name: ").strip()
        if not raw_value:
            break
        lambda_names.append(raw_value)

    if lambda_names:
        return tuple(lambda_names)
    return default_lambda_names(env_name)


def collect_inputs() -> ReviewInputs:
    """Prompt for review inputs and validate the result."""

    env_name = prompt_with_default("Environment", "prod")
    region = prompt_with_default("AWS region", os.getenv("AWS_REGION", "us-west-1"))
    start_time = prompt_with_default("Start time (UTC ISO 8601)", default_start_time())
    end_time = prompt_with_default("End time (UTC ISO 8601)", default_end_time())
    start_dt = parse_iso8601(start_time)
    end_dt = parse_iso8601(end_time)

    if start_dt >= end_dt:
        raise AutomationError("Start time must be earlier than end time.")

    lambda_names = prompt_lambda_names(env_name)
    return ReviewInputs(
        env_name=env_name,
        start_time=start_time,
        end_time=end_time,
        region=region,
        lambda_names=lambda_names,
    )


def build_run_directory(repo_root: Path, inputs: ReviewInputs) -> Path:
    """Compute the artifact run directory used by the shell collector."""

    run_id = (
        f"{inputs.env_name}-"
        f"{sanitize_for_path(inputs.start_time)}_"
        f"{sanitize_for_path(inputs.end_time)}"
    )
    return repo_root / "artifacts" / "cloudwatch-review" / run_id


def run_collector(repo_root: Path, inputs: ReviewInputs) -> Path:
    """Run the existing shell collector and return its artifact directory."""

    collector_path = repo_root / "scripts" / "cloudwatch_review_collect.sh"
    run_dir = build_run_directory(repo_root, inputs)

    command = [
        "bash",
        str(collector_path),
        "--env",
        inputs.env_name,
        "--start",
        inputs.start_time,
        "--end",
        inputs.end_time,
        "--region",
        inputs.region,
    ]
    for lambda_name in inputs.lambda_names:
        command.extend(["--lambda", lambda_name])

    LOGGER.info("Running collector script...")
    subprocess.run(command, cwd=repo_root, check=True)

    if not run_dir.exists():
        raise AutomationError(f"Expected artifact directory was not created: {run_dir}")
    return run_dir


def read_text_file(path: Path) -> str:
    """Read a UTF-8 text file from disk."""

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AutomationError(f"Required file not found: {path}") from exc


def parse_metadata_details(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Parse metadata scalars and section lists."""

    scalars: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in read_text_file(path).splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        if stripped_line.startswith("[") and stripped_line.endswith("]"):
            current_section = stripped_line[1:-1]
            sections[current_section] = []
            continue
        if current_section:
            sections[current_section].append(stripped_line)
            continue
        if "=" in stripped_line:
            key, value = stripped_line.split("=", 1)
            scalars[key] = value

    return scalars, sections


def extract_json_object(text: str, start_index: int = 0) -> tuple[Any, int]:
    """Extract the first JSON object found after a starting index."""

    json_start = text.find("{", start_index)
    if json_start == -1:
        raise AutomationError("Expected JSON object was not found in artifact content.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(json_start, len(text)):
        character = text[index]
        escaped, in_string, handled = update_json_string_state(character, escaped, in_string)
        if handled:
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[json_start : index + 1]), index + 1

    raise AutomationError("Artifact JSON object was incomplete.")


def split_lambda_chunks(text: str) -> list[str]:
    """Split an artifact file into Lambda-specific chunks."""

    return [chunk.strip() for chunk in text.split(SECTION_SEPARATOR) if LAMBDA_PREFIX in chunk]


def update_json_string_state(character: str, escaped: bool, in_string: bool) -> tuple[bool, bool, bool]:
    """Update string/escape state while scanning embedded JSON."""

    if escaped:
        return False, in_string, True
    if character == "\\":
        return True, in_string, True
    if character == '"':
        return False, not in_string, True
    return False, in_string, False


def lambda_name_from_chunk(chunk: str) -> str:
    """Extract a Lambda name from a separated artifact chunk."""

    for line in chunk.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith(LAMBDA_PREFIX):
            return stripped_line.replace(LAMBDA_PREFIX, "", 1).strip()
    raise AutomationError("Expected Lambda name was not found in artifact chunk.")


def parse_lambda_configs(path: Path) -> dict[str, dict[str, Any]]:
    """Parse Lambda configuration JSON blocks."""

    configs: dict[str, dict[str, Any]] = {}
    for chunk in split_lambda_chunks(read_text_file(path)):
        lambda_name = lambda_name_from_chunk(chunk)
        config_json, _ = extract_json_object(chunk)
        configs[lambda_name] = config_json
    return configs


def extract_json_after_marker(chunk: str, marker: str) -> dict[str, Any]:
    """Extract the first JSON object after a marker within a chunk."""

    marker_index = chunk.find(marker)
    if marker_index == -1:
        raise AutomationError(f"Expected marker '{marker}' was not found.")
    parsed_json, _ = extract_json_object(chunk, marker_index)
    return parsed_json


def sum_metric_values(metric_payload: dict[str, Any], value_field: str = "Sum") -> int:
    """Sum numeric values from CloudWatch datapoints."""

    total = 0.0
    for datapoint in metric_payload.get("Datapoints", []):
        total += float(datapoint.get(value_field, 0.0))
    return int(round(total))


def max_metric_value(metric_payload: dict[str, Any], value_field: str) -> float | None:
    """Return the maximum numeric datapoint value."""

    values = [float(datapoint.get(value_field, 0.0)) for datapoint in metric_payload.get("Datapoints", [])]
    return max(values) if values else None


def parse_invocation_metrics(path: Path) -> dict[str, dict[str, int]]:
    """Parse invocation, error, and throttle totals by Lambda."""

    metrics: dict[str, dict[str, int]] = {}
    for chunk in split_lambda_chunks(read_text_file(path)):
        lambda_name = lambda_name_from_chunk(chunk)
        invocation_payload = extract_json_after_marker(chunk, "-- Invocations --")
        error_payload = extract_json_after_marker(chunk, "-- Errors --")
        throttle_payload = extract_json_after_marker(chunk, "-- Throttles --")
        metrics[lambda_name] = {
            "invocations": sum_metric_values(invocation_payload),
            "errors": sum_metric_values(error_payload),
            "throttles": sum_metric_values(throttle_payload),
        }
    return metrics


def parse_concurrency_metrics(path: Path) -> dict[str, float | None]:
    """Parse peak concurrency by Lambda."""

    concurrency: dict[str, float | None] = {}
    for chunk in split_lambda_chunks(read_text_file(path)):
        lambda_name = lambda_name_from_chunk(chunk)
        payload, _ = extract_json_object(chunk)
        concurrency[lambda_name] = max_metric_value(payload, "Maximum")
    return concurrency


def lambda_name_from_log_identifier(log_identifier: str) -> str:
    """Extract a Lambda name from a CloudWatch log identifier."""

    return log_identifier.split("/aws/lambda/")[-1]


def row_to_mapping(row: list[dict[str, str]]) -> dict[str, str]:
    """Convert a Logs Insights row into a simple mapping."""

    return {entry["field"]: entry["value"] for entry in row}


def parse_logs_json_payload(path: Path) -> dict[str, Any]:
    """Parse the JSON payload embedded in a logs artifact file."""

    payload, _ = extract_json_object(read_text_file(path))
    if not isinstance(payload, dict):
        raise AutomationError(f"Unexpected JSON payload in {path}.")
    return payload


def parse_report_metrics(path: Path) -> dict[str, dict[str, float | int]]:
    """Parse REPORT-derived metrics by Lambda."""

    metrics: dict[str, dict[str, float | int]] = {}
    payload = parse_logs_json_payload(path)
    for row in payload.get("results", []):
        mapped_row = row_to_mapping(row)
        lambda_name = lambda_name_from_log_identifier(mapped_row["@log"])
        metrics[lambda_name] = {
            "avg_duration_ms": float(mapped_row["avg_duration_ms"]),
            "p95_duration_ms": float(mapped_row["p95_duration_ms"]),
            "p99_duration_ms": float(mapped_row["p99_duration_ms"]),
            "max_duration_ms": float(mapped_row["max_duration_ms"]),
            "cold_starts": int(float(mapped_row["cold_starts"])),
            "avg_init_duration_ms": float(mapped_row["avg_init_duration_ms"]) if mapped_row.get("avg_init_duration_ms") not in {"None", "null", ""} else 0.0,
        }
    return metrics


def parse_error_counts(path: Path) -> dict[str, dict[str, int]]:
    """Parse warning and error counts by Lambda."""

    counts: dict[str, dict[str, int]] = {}
    payload = parse_logs_json_payload(path)
    for row in payload.get("results", []):
        mapped_row = row_to_mapping(row)
        lambda_name = lambda_name_from_log_identifier(mapped_row["@log"])
        counts.setdefault(lambda_name, {"WARNING": 0, "ERROR": 0})
        counts[lambda_name][mapped_row["level"]] = int(mapped_row["total"])
    return counts


def parse_top_messages(path: Path) -> list[dict[str, str | int]]:
    """Parse top warning and error messages."""

    messages: list[dict[str, str | int]] = []
    payload = parse_logs_json_payload(path)
    for row in payload.get("results", []):
        mapped_row = row_to_mapping(row)
        messages.append(
            {
                "lambda_name": lambda_name_from_log_identifier(mapped_row["@log"]),
                "level": mapped_row.get("level", "UNKNOWN"),
                "message": mapped_row.get("app_message", "(message unavailable)"),
                "total": int(mapped_row.get("total", "0")),
            }
        )
    return messages


def load_lambda_summaries(run_dir: Path) -> tuple[dict[str, str], dict[str, list[str]], dict[str, LambdaSummary], list[dict[str, str | int]]]:
    """Load and merge all artifact-derived Lambda summary data."""

    metadata_scalars, metadata_sections = parse_metadata_details(run_dir / "metadata.txt")
    lambda_names = metadata_sections.get("active_lambdas") or metadata_sections.get("requested_lambdas") or ()

    config_data = parse_lambda_configs(run_dir / "config.txt")
    invocation_metrics = parse_invocation_metrics(run_dir / "metrics_invocations.txt")
    concurrency_metrics = parse_concurrency_metrics(run_dir / "metrics_concurrency.txt")
    report_metrics = parse_report_metrics(run_dir / "logs_report.txt")
    error_counts = parse_error_counts(run_dir / "logs_error_counts.txt")
    top_messages = parse_top_messages(run_dir / "logs_top_messages.txt")

    summaries: dict[str, LambdaSummary] = {}
    for lambda_name in lambda_names:
        config = config_data.get(lambda_name, {})
        report = report_metrics.get(lambda_name, {})
        invocations = invocation_metrics.get(lambda_name, {})
        warnings_and_errors = error_counts.get(lambda_name, {})
        summaries[lambda_name] = LambdaSummary(
            name=lambda_name,
            invocations=invocations.get("invocations"),
            lambda_errors=invocations.get("errors"),
            throttles=invocations.get("throttles"),
            avg_duration_ms=float(report["avg_duration_ms"]) if "avg_duration_ms" in report else None,
            p95_duration_ms=float(report["p95_duration_ms"]) if "p95_duration_ms" in report else None,
            p99_duration_ms=float(report["p99_duration_ms"]) if "p99_duration_ms" in report else None,
            max_duration_ms=float(report["max_duration_ms"]) if "max_duration_ms" in report else None,
            cold_starts=int(report["cold_starts"]) if "cold_starts" in report else None,
            avg_init_duration_ms=float(report["avg_init_duration_ms"]) if "avg_init_duration_ms" in report else None,
            warning_count=warnings_and_errors.get("WARNING", 0),
            error_count=warnings_and_errors.get("ERROR", 0),
            max_concurrency=concurrency_metrics.get(lambda_name),
            in_vpc=bool(config.get("VpcConfig", {}).get("VpcId")),
            state=config.get("State"),
            update_status=config.get("LastUpdateStatus"),
        )

    return metadata_scalars, metadata_sections, summaries, top_messages


def format_count(value: int | None) -> str:
    """Format an integer count for report output."""

    return "unavailable" if value is None else f"{value}"


def format_ms(value: float | None) -> str:
    """Format a millisecond value for report output."""

    return "unavailable" if value is None else f"{value:.2f} ms"


def choose_primary_lambda(summaries: dict[str, LambdaSummary]) -> LambdaSummary | None:
    """Choose the Lambda that most deserves focus in the summary."""

    if not summaries:
        return None

    def score(summary: LambdaSummary) -> float:
        log_signal = (summary.error_count * 100.0) + (summary.warning_count * 10.0)
        latency_signal = (summary.p95_duration_ms or 0.0) + (summary.avg_init_duration_ms or 0.0)
        service_signal = float((summary.lambda_errors or 0) + (summary.throttles or 0)) * 1000.0
        return log_signal + latency_signal + service_signal

    return max(summaries.values(), key=score)


def build_lambda_list(lambda_names: list[str]) -> str:
    """Build the markdown Lambda list."""

    return "\n".join(f"- `{lambda_name}`" for lambda_name in lambda_names)


def build_metrics_table(lambda_names: list[str], summaries: dict[str, LambdaSummary]) -> str:
    """Build the markdown metrics table rows."""

    rows: list[str] = []
    for lambda_name in lambda_names:
        summary = summaries[lambda_name]
        rows.append(
            "| "
            f"`{lambda_name}` | "
            f"{format_count(summary.invocations)} | "
            f"{format_count(summary.lambda_errors)} | "
            f"{format_count(summary.throttles)} | "
            f"{format_ms(summary.avg_duration_ms)} | "
            f"{format_ms(summary.p95_duration_ms)} | "
            f"{format_ms(summary.p99_duration_ms)} | "
            f"{format_ms(summary.max_duration_ms)} | "
            f"{format_count(summary.cold_starts)} | "
            f"{format_ms(summary.avg_init_duration_ms)} |"
        )
    return "\n".join(rows)


def build_additional_observations(lambda_names: list[str], summaries: dict[str, LambdaSummary], missing_lambdas: list[str]) -> str:
    """Build the additional observations bullet list."""

    bullets: list[str] = []
    peak_concurrency = max((summaries[name].max_concurrency or 0.0) for name in lambda_names) if lambda_names else 0.0
    if peak_concurrency <= 2:
        bullets.append("- Concurrency stayed low across the reviewed Lambdas, so load-driven contention looks unlikely.")
    else:
        bullets.append(f"- Peak observed concurrency reached `{peak_concurrency:.0f}` across the reviewed Lambdas.")

    if lambda_names and all(summaries[name].in_vpc for name in lambda_names):
        bullets.append("- All reviewed Lambdas run inside a VPC.")

    if lambda_names and all(summaries[name].state == "Active" and summaries[name].update_status == "Successful" for name in lambda_names):
        bullets.append("- Lambda configuration state looked healthy: all reviewed functions were `Active` with successful last updates.")

    if missing_lambdas:
        bullets.append("- Some requested Lambdas were missing and therefore excluded from metric/log review.")

    if not bullets:
        bullets.append("- No additional infrastructure-level observations were derived from the artifact set.")
    return "\n".join(bullets)


def build_error_summary(lambda_names: list[str], summaries: dict[str, LambdaSummary]) -> str:
    """Build the application error summary section."""

    nonzero = [summaries[name] for name in lambda_names if summaries[name].warning_count or summaries[name].error_count]
    if not nonzero:
        return "No warning or error log counts were returned for the reviewed Lambdas in the selected window."

    lines = ["Warning/error totals by Lambda:"]
    for lambda_name in lambda_names:
        summary = summaries[lambda_name]
        lines.append(
            f"- `{lambda_name}`: `{summary.warning_count} WARNING`, `{summary.error_count} ERROR`"
        )
    return "\n".join(lines)


def build_top_messages(top_messages: list[dict[str, str | int]]) -> str:
    """Build the top recurring messages list."""

    if not top_messages:
        return "- No recurring warning/error messages were returned."

    lines: list[str] = []
    for message in top_messages[:8]:
        lines.append(
            f"- `{message['lambda_name']}`: `{message['level']} {message['message']}`: `{message['total']}`"
        )
    return "\n".join(lines)


def build_executive_summary(lambda_names: list[str], summaries: dict[str, LambdaSummary], primary: LambdaSummary | None) -> str:
    """Build the executive summary section."""

    if primary is None:
        return "- No active Lambda summaries were available from the artifact set."

    nonzero_logs = [summaries[name] for name in lambda_names if summaries[name].warning_count or summaries[name].error_count]
    healthy_rest = [
        summaries[name]
        for name in lambda_names
        if name != primary.name and summaries[name].error_count == 0 and summaries[name].warning_count == 0
    ]

    lines: list[str] = [build_health_opening(primary.name, len(nonzero_logs))]

    if all((summaries[name].lambda_errors or 0) == 0 and (summaries[name].throttles or 0) == 0 for name in lambda_names):
        lines.append("- Lambda service-level metrics were clean: no returned `Errors` and no returned `Throttles` for the reviewed functions.")

    if healthy_rest:
        healthy_names = ", ".join(f"`{summary.name}`" for summary in healthy_rest[:2])
        lines.append(f"- {healthy_names} look comparatively healthy and stable in this window.")

    issue_fragments = collect_issue_fragments(primary)
    if issue_fragments:
        lines.append(f"- `{primary.name}` stands out for " + ", ".join(issue_fragments) + ".")

    return "\n".join(lines)


def build_rca_summary(primary: LambdaSummary | None, top_messages: list[dict[str, str | int]]) -> str:
    """Build the RCA summary section."""

    if primary is None:
        return "No RCA summary could be derived because no active Lambda summaries were available."

    deduped_themes = infer_primary_themes(primary.name, top_messages)
    lines = [f"`{primary.name}` is the dominant Lambda in this review based on the collected metrics and log signals."]

    if primary.avg_init_duration_ms and primary.avg_init_duration_ms > 500:
        lines.append(
            f"Cold starts are a real contributor, with average init around `{primary.avg_init_duration_ms:.2f} ms`."
        )

    if primary.p95_duration_ms and primary.avg_duration_ms and primary.p95_duration_ms > (primary.avg_duration_ms * 3):
        lines.append(
            "The tail-latency gap between average and p95 duration suggests that a subset of requests is much slower than the median execution path."
        )

    if deduped_themes:
        lines.append("The most likely application-level contributors are " + ", ".join(deduped_themes) + ".")
    else:
        lines.append("The artifact set points to application-level behavior rather than Lambda platform instability, but the exact failure mode is not explicit in the top messages.")

    return "\n\n".join(lines)


def build_health_opening(primary_name: str, nonzero_log_count: int) -> str:
    """Build the opening executive-summary sentence."""

    if nonzero_log_count == 0:
        return "- The reviewed Lambdas look healthy at both the Lambda service level and the application-log summary level."
    if nonzero_log_count == 1:
        return f"- The main issue is isolated to `{primary_name}`."
    return f"- `{primary_name}` is the most material issue in this review window, but multiple Lambdas show application-level warning/error signals."


def collect_issue_fragments(primary: LambdaSummary) -> list[str]:
    """Collect the key issue fragments for a primary Lambda."""

    issue_fragments: list[str] = []
    if primary.p95_duration_ms and primary.avg_duration_ms and primary.p95_duration_ms > (primary.avg_duration_ms * 3):
        issue_fragments.append("significant tail latency")
    if primary.error_count or primary.warning_count:
        issue_fragments.append("application warning/error logs")
    if primary.avg_init_duration_ms and primary.avg_init_duration_ms > 500:
        issue_fragments.append("expensive cold starts")
    return issue_fragments


def infer_primary_themes(primary_name: str, top_messages: list[dict[str, str | int]]) -> list[str]:
    """Infer RCA themes from the primary Lambda's top messages."""

    themes: list[str] = []
    for message in top_messages:
        if message["lambda_name"] != primary_name:
            continue
        message_text = str(message["message"])
        if "llm_streaming" in message_text or "retries_exhausted" in message_text:
            themes.append("runtime streaming/retry instability")
        if "should_flush" in message_text:
            themes.append("buffering or flush-path stress")
        if "brand_config.validation_failed" in message_text:
            themes.append("brand configuration validation failures")
        if "LRS not configured" in message_text or "Non-INSERT operation" in message_text:
            themes.append("expected or misclassified filtering/skip paths")
        if "CDC payload missing data block" in message_text:
            themes.append("input payload quality issues")
    return list(dict.fromkeys(themes))


def build_next_steps(primary: LambdaSummary | None, top_messages: list[dict[str, str | int]]) -> str:
    """Build the next steps section."""

    steps: list[str] = []
    message_text = " ".join(str(message["message"]) for message in top_messages if primary and message["lambda_name"] == primary.name)

    if "llm_streaming" in message_text or "retries_exhausted" in message_text:
        steps.append("1. Inspect the streaming/retry path for the affected Lambda and review the exact failures around the `llm_streaming` and retry exhaustion messages.")
        steps.append("2. Review downstream provider latency and timeout behavior for the affected request path.")
    if "brand_config.validation_failed" in message_text:
        steps.append("1. Review brand configuration inputs and validation rules for the publisher path that is producing `brand_config.validation_failed`.")
    if "LRS not configured" in message_text or "Non-INSERT operation" in message_text:
        steps.append("2. Separate expected skip conditions from real defects so warning/error logs better represent actionable failures.")
    if primary and primary.avg_init_duration_ms and primary.avg_init_duration_ms > 500:
        steps.append("3. Review init-time work and dependency startup cost to reduce cold-start overhead.")
    if primary and primary.p95_duration_ms and primary.avg_duration_ms and primary.p95_duration_ms > (primary.avg_duration_ms * 3):
        steps.append("4. Inspect slow-request traces or logs around the worst p95/p99 executions to identify the expensive execution path.")

    if not steps:
        steps = [
            "1. Spot-check the top warning/error messages in CloudWatch logs to confirm whether they represent expected behavior or user-facing issues.",
            "2. Review the slowest functions by p95 duration and cold-start cost for any straightforward configuration or initialization wins.",
            "3. Keep this report as a baseline and compare against the next collection window for regressions.",
        ]

    deduped_steps = list(dict.fromkeys(steps))
    return "\n".join(deduped_steps[:5])


def build_notes(metadata_sections: dict[str, list[str]]) -> str:
    """Build the notes section."""

    notes = [
        "- `Lambda Errors = 0` does not necessarily mean the application path was healthy; warning/error logs can still indicate degraded behavior.",
        "- `REPORT` memory values are useful as comparative signals, but they may reflect raw reported units rather than normalized MiB values.",
    ]
    missing = metadata_sections.get("missing_lambdas", [])
    if missing and missing != ["none"]:
        notes.append("- Some requested Lambdas were missing from the environment and therefore excluded from the generated report.")
    return "\n".join(notes)


def render_report(template_text: str, replacements: dict[str, str]) -> str:
    """Render the markdown report from the template."""

    rendered = template_text
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered.rstrip() + "\n"


def build_report(repo_root: Path, run_dir: Path) -> tuple[str, str, str]:
    """Build the final report markdown and return metadata for output."""

    metadata_scalars, metadata_sections, summaries, top_messages = load_lambda_summaries(run_dir)
    lambda_names = metadata_sections.get("active_lambdas") or metadata_sections.get("requested_lambdas") or []
    if lambda_names == ["none"]:
        lambda_names = []

    template_text = read_text_file(repo_root / "docs" / "templates" / "lambda_cloudwatch_review.template.md")
    primary = choose_primary_lambda(summaries)

    replacements = {
        "START_TIME": metadata_scalars.get("start_time", "unavailable"),
        "END_TIME": metadata_scalars.get("end_time", "unavailable"),
        "ENV_NAME": metadata_scalars.get("env", "unavailable"),
        "LAMBDA_LIST": build_lambda_list(lambda_names),
        "EXECUTIVE_SUMMARY": build_executive_summary(lambda_names, summaries, primary),
        "KEY_METRICS_TABLE": build_metrics_table(lambda_names, summaries),
        "ADDITIONAL_OBSERVATIONS": build_additional_observations(
            lambda_names,
            summaries,
            metadata_sections.get("missing_lambdas", []),
        ),
        "ERROR_SUMMARY": build_error_summary(lambda_names, summaries),
        "TOP_MESSAGES": build_top_messages(top_messages),
        "RCA_SUMMARY": build_rca_summary(primary, top_messages),
        "NEXT_STEPS": build_next_steps(primary, top_messages),
        "NOTES": build_notes(metadata_sections),
    }
    execution_date = parse_iso8601(metadata_scalars["end_time"]).date().isoformat()
    return metadata_scalars["env"], execution_date, render_report(template_text, replacements)


def save_report(repo_root: Path, env_name: str, execution_date: str, markdown_text: str) -> Path:
    """Write the generated report markdown to the reports directory."""

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / REPORT_FILENAME_TEMPLATE.format(
        env=env_name,
        execution_date=execution_date,
    )
    output_path.write_text(markdown_text, encoding="utf-8")
    return output_path


def main() -> int:
    """Run the interactive CloudWatch review workflow."""

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv_file(repo_root / ".env")

    try:
        print("CloudWatch Review Automation\n")
        inputs = collect_inputs()
        run_dir = run_collector(repo_root, inputs)
        LOGGER.info("Generating markdown report from collected artifacts...")
        env_name, execution_date, markdown_text = build_report(repo_root, run_dir)
        output_path = save_report(repo_root, env_name, execution_date, markdown_text)
    except AutomationError as exc:
        LOGGER.error("Error: %s", exc)
        return 1
    except subprocess.CalledProcessError as exc:
        LOGGER.error("Collector script failed with exit code %s.", exc.returncode)
        return exc.returncode or 1
    except KeyboardInterrupt:
        LOGGER.error("\nCancelled by user.")
        return 130

    print("\nReview generation complete.")
    print(f"Artifacts: {run_dir}")
    print(f"Report: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
