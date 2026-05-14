"""Tests for the CloudWatch review runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_review_module() -> ModuleType:
    """Load the review runner module from the scripts directory."""

    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_cloudwatch_review.py"
    )
    spec = importlib.util.spec_from_file_location("run_cloudwatch_review", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load run_cloudwatch_review.py for testing.")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_filtered_report_metrics_reads_p90(tmp_path: Path) -> None:
    """Parse the filtered Logs Insights payload into lambda summaries."""

    module = load_review_module()
    payload_path = tmp_path / "logs_filtered_report.txt"
    payload_path.write_text(
        "\n".join(
            [
                "QueryId: example-query-id",
                "Status: Complete",
                "",
                '{"results":[['
                '{"field":"@log","value":"/aws/lambda/zen-prod-sqs-message-consumer"},'
                '{"field":"matching_invocations","value":"12"},'
                '{"field":"avg_duration_ms","value":"115.5"},'
                '{"field":"p90_duration_ms","value":"220.0"},'
                '{"field":"p95_duration_ms","value":"240.0"},'
                '{"field":"p99_duration_ms","value":"300.0"},'
                '{"field":"max_duration_ms","value":"330.0"}'
                ']],"statistics":{"recordsMatched":1.0}}',
            ]
        ),
        encoding="utf-8",
    )

    metrics = module.parse_filtered_report_metrics(payload_path)

    summary = metrics["zen-prod-sqs-message-consumer"]
    assert summary.matching_invocations == 12
    assert summary.avg_duration_ms == pytest.approx(115.5)
    assert summary.p90_duration_ms == pytest.approx(220.0)
    assert summary.p95_duration_ms == pytest.approx(240.0)
    assert summary.p99_duration_ms == pytest.approx(300.0)
    assert summary.max_duration_ms == pytest.approx(330.0)


def test_parse_filter_values_deduplicates_values() -> None:
    """Normalize a comma-separated filter list into unique values."""

    module = load_review_module()

    assert module.parse_filter_values("brand-1, brand-2, brand-1, , brand-3") == (
        "brand-1",
        "brand-2",
        "brand-3",
    )


def test_build_filtered_metrics_section_renders_table() -> None:
    """Render the filtered latency section with a p90 column."""

    module = load_review_module()
    filtered_metrics = {
        "zen-prod-sqs-message-consumer": module.FilteredLambdaSummary(
            name="zen-prod-sqs-message-consumer",
            matching_invocations=8,
            avg_duration_ms=100.0,
            p90_duration_ms=160.0,
            p95_duration_ms=190.0,
            p99_duration_ms=250.0,
            max_duration_ms=320.0,
        )
    }

    rendered = module.build_filtered_metrics_section(
        ["zen-prod-sqs-message-consumer", "zen-prod-ws-to-sqs-producer"],
        {
            "message_filter_key": "brand_id",
        },
        {
            "message_filter_values": ["brand-42", "brand-99"],
        },
        filtered_metrics,
    )

    assert "`brand_id` in [`brand-42`, `brand-99`]" in rendered
    assert "| Lambda | Matching Invocations | Avg Duration | P90 | P95 | P99 | Max |" in rendered
    assert "`zen-prod-sqs-message-consumer` | 8 | 100.00 ms | 160.00 ms" in rendered
    assert "No matching invocations were returned for: `zen-prod-ws-to-sqs-producer`." in rendered


def test_build_filtered_metrics_section_handles_missing_filter() -> None:
    """Return a fallback message when no filter was requested."""

    module = load_review_module()

    rendered = module.build_filtered_metrics_section(
        ["zen-prod-sqs-message-consumer"],
        {
            "message_filter_key": "",
        },
        {},
        {},
    )

    assert rendered == "No message-level latency filter was requested for this review."


@pytest.mark.parametrize(
    "preset, days_offset",
    [("daily", 0), ("weekly", 7), ("biweekly", 14), ("monthly", 30)],
)
def test_resolve_time_range_anchors_to_today_midnight(preset: str, days_offset: int) -> None:
    """Each preset anchors its start at the UTC midnight N days before today."""

    from datetime import UTC, datetime, timedelta

    module = load_review_module()
    frozen_now = datetime(2026, 5, 13, 21, 33, 47, tzinfo=UTC)
    start_iso, end_iso = module.resolve_time_range(preset, now=frozen_now)

    expected_start = (
        frozen_now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=days_offset)
    )
    expected_end = frozen_now.replace(microsecond=0)
    assert start_iso == expected_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert end_iso == expected_end.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_resolve_time_range_rejects_custom_and_unknown() -> None:
    """resolve_time_range only accepts the named presets, not 'custom'."""

    module = load_review_module()
    with pytest.raises(module.AutomationError):
        module.resolve_time_range("custom")
    with pytest.raises(module.AutomationError):
        module.resolve_time_range("hourly")


def test_is_ignored_message_case_insensitive_substring() -> None:
    """The ignore predicate matches case-insensitive substrings."""

    module = load_review_module()
    patterns = ("should_flush.emergency_accumulation",)
    assert module.is_ignored_message("ERROR should_flush.emergency_accumulation", patterns)
    assert module.is_ignored_message("Should_Flush.Emergency_Accumulation triggered", patterns)
    assert not module.is_ignored_message("llm_streaming.failed", patterns)
    assert not module.is_ignored_message("", patterns)
    assert not module.is_ignored_message("anything", ())


def test_apply_ignore_filter_drops_emergency_accumulation() -> None:
    """The default ignore list strips emergency_accumulation and adjusts counts."""

    module = load_review_module()
    summaries = {
        "zen-prod-sqs-message-consumer": module.LambdaSummary(
            name="zen-prod-sqs-message-consumer",
            warning_count=1,
            error_count=8,
        ),
        "zen-prod-ws-to-sqs-producer": module.LambdaSummary(
            name="zen-prod-ws-to-sqs-producer",
            warning_count=0,
            error_count=0,
        ),
    }
    top_messages = [
        {
            "lambda_name": "zen-prod-sqs-message-consumer",
            "level": "ERROR",
            "message": "should_flush.emergency_accumulation",
            "total": 8,
        },
        {
            "lambda_name": "zen-prod-sqs-message-consumer",
            "level": "WARNING",
            "message": "stream_runner.llm_streaming.failed",
            "total": 1,
        },
    ]

    kept, adjusted = module.apply_ignore_filter(
        top_messages, summaries, module.DEFAULT_IGNORED_MESSAGES
    )

    assert [row["message"] for row in kept] == ["stream_runner.llm_streaming.failed"]
    assert adjusted["zen-prod-sqs-message-consumer"].error_count == 0
    assert adjusted["zen-prod-sqs-message-consumer"].warning_count == 1
    assert adjusted["zen-prod-ws-to-sqs-producer"].error_count == 0


def test_apply_ignore_filter_floors_at_zero() -> None:
    """Subtracting a count larger than the per-lambda total never goes negative."""

    module = load_review_module()
    summaries = {
        "lambda-a": module.LambdaSummary(name="lambda-a", warning_count=0, error_count=2),
    }
    top_messages = [
        {"lambda_name": "lambda-a", "level": "ERROR", "message": "noise", "total": 50},
    ]

    _, adjusted = module.apply_ignore_filter(top_messages, summaries, ("noise",))
    assert adjusted["lambda-a"].error_count == 0


def test_load_ignored_messages_merges_defaults_env_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Combined ignore list preserves order and deduplicates entries."""

    module = load_review_module()
    monkeypatch.setenv(module.IGNORED_MESSAGES_ENV_VAR, "from_env, should_flush.emergency_accumulation")

    merged = module.load_ignored_messages(cli_patterns=("from_cli", "from_env"))
    assert merged[0] == "should_flush.emergency_accumulation"
    assert "from_env" in merged
    assert "from_cli" in merged
    assert merged.count("from_env") == 1

    monkeypatch.delenv(module.IGNORED_MESSAGES_ENV_VAR, raising=False)
    without_defaults = module.load_ignored_messages(
        cli_patterns=("only_cli",), use_defaults=False
    )
    assert "should_flush.emergency_accumulation" not in without_defaults
    assert without_defaults == ("only_cli",)


def test_write_lambda_csv_schema_and_rows(tmp_path: Path) -> None:
    """Lambda CSV writer emits the expected header order and row content."""

    module = load_review_module()
    repo_root = tmp_path
    summaries = {
        "zen-prod-sqs-message-consumer": module.LambdaSummary(
            name="zen-prod-sqs-message-consumer",
            invocations=571,
            lambda_errors=0,
            throttles=0,
            avg_duration_ms=760.15,
            p95_duration_ms=3354.11,
            p99_duration_ms=5222.44,
            max_duration_ms=7015.35,
            cold_starts=19,
            avg_init_duration_ms=1605.81,
            warning_count=1,
            error_count=0,
        ),
        "zen-prod-ws-to-sqs-producer": module.LambdaSummary(
            name="zen-prod-ws-to-sqs-producer",
            invocations=571,
            lambda_errors=0,
            throttles=0,
            avg_duration_ms=15.56,
            p95_duration_ms=58.03,
            p99_duration_ms=73.17,
            max_duration_ms=121.00,
            cold_starts=13,
            avg_init_duration_ms=503.07,
            warning_count=0,
            error_count=0,
        ),
    }
    top_messages = [
        {
            "lambda_name": "zen-prod-sqs-message-consumer",
            "level": "WARNING",
            "message": "stream_runner.llm_streaming.failed",
            "total": 1,
        },
    ]

    csv_path = module.write_lambda_csv(
        repo_root,
        env_name="prod",
        execution_date="2026-05-14",
        lambda_names=[
            "zen-prod-sqs-message-consumer",
            "zen-prod-ws-to-sqs-producer",
        ],
        summaries=summaries,
        top_messages=top_messages,
    )

    assert csv_path == tmp_path / "reports" / "prod_2026-05-14_lambda_metrics.csv"
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == ",".join(module.LAMBDA_CSV_COLUMNS)

    first_row = lines[1].split(",")
    assert first_row[0] == "zen-prod-sqs-message-consumer"
    assert first_row[1] == "571"
    assert first_row[4] == "760.15"
    assert first_row[10] == "1"
    assert first_row[11] == "0"
    # top_warnings is the trailing column for this lambda.
    assert lines[1].endswith("stream_runner.llm_streaming.failed:1")
    # No warnings/errors -> blank top_messages on the second row.
    assert lines[2].endswith(",,")


def test_encode_top_messages_strips_separators() -> None:
    """Encoded top messages strip colons/semicolons so the format stays parsable."""

    module = load_review_module()
    encoded = module.encode_top_messages_for_csv(
        [
            {"lambda_name": "lambda-a", "level": "ERROR", "message": "a:b;c", "total": 3},
            {"lambda_name": "lambda-a", "level": "ERROR", "message": "second", "total": 5},
        ],
        "lambda-a",
        "ERROR",
    )
    # Pre-sorted by total desc; colons and semicolons in messages are replaced
    # with spaces so the "msg:count; msg:count" grammar is unambiguous.
    assert encoded == "second:5; a b c:3"


def test_build_notes_includes_ignored_patterns_when_provided() -> None:
    """The notes section mentions any active ignore patterns."""

    module = load_review_module()
    notes = module.build_notes({}, {}, ignored_messages=("should_flush.emergency_accumulation",))
    assert "Ignored noise patterns" in notes
    assert "should_flush.emergency_accumulation" in notes
