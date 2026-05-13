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
