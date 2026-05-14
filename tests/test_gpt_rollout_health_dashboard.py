"""Tests for the GPT rollout health dashboard."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest


def _load_dashboard_module() -> ModuleType:
    """Load the rollout health dashboard module from the scripts directory."""

    module_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "gpt_rollout_health_dashboard.py"
    )
    spec = importlib.util.spec_from_file_location(
        "gpt_rollout_health_dashboard", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load gpt_rollout_health_dashboard.py for testing.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dashboard() -> ModuleType:
    """Provide the dashboard module to test functions."""

    return _load_dashboard_module()


def _write_pinot_csv(path: Path) -> Path:
    """Write a well-formed Pinot metrics CSV and return its path."""

    path.write_text(
        "brand_id,total_requests,avg_ttfb_ms,p50_ttfb_ms,p95_ttfb_ms,p99_ttfb_ms\n"
        "brand-a,1000,1500,1300,3800,5500\n"
        "brand-b,800,3200,2900,6500,9200\n"
        "brand-c,500,4000,3500,9500,17000\n",
        encoding="utf-8",
    )
    return path


def _write_mysql_csv(path: Path) -> Path:
    """Write a well-formed MySQL metrics CSV and return its path."""

    path.write_text(
        "brand_id,brand_name,total_sessions,total_utterances,avg_utterances_per_session\n"
        "brand-a,Acme,2000,30000,15.0\n"
        "brand-b,Beta Co,1500,21000,14.0\n"
        "brand-c,Gamma,500,4500,9.0\n",
        encoding="utf-8",
    )
    return path


def test_load_pinot_metrics_reads_required_columns(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """Pinot loader should return a frame with the required columns and numeric types."""

    csv_path = _write_pinot_csv(tmp_path / "pinot.csv")
    frame = dashboard.load_pinot_metrics(csv_path)
    assert list(frame.columns) == list(dashboard.PINOT_REQUIRED_COLUMNS)
    assert len(frame) == 3
    assert pd.api.types.is_numeric_dtype(frame["p95_ttfb_ms"])


def test_load_pinot_metrics_missing_file_raises(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """A missing Pinot CSV must raise DashboardError, not a low-level IOError."""

    with pytest.raises(dashboard.DashboardError):
        dashboard.load_pinot_metrics(tmp_path / "missing.csv")


def test_load_pinot_metrics_missing_column_raises(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """A Pinot CSV missing a required column should raise MissingColumnsError."""

    csv_path = tmp_path / "pinot_bad.csv"
    csv_path.write_text(
        "brand_id,total_requests,avg_ttfb_ms,p50_ttfb_ms,p95_ttfb_ms\n"
        "brand-a,1,2,3,4\n",
        encoding="utf-8",
    )
    with pytest.raises(dashboard.MissingColumnsError):
        dashboard.load_pinot_metrics(csv_path)


def test_load_mysql_metrics_reads_required_columns(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """MySQL loader should return the required columns and trim string brand_id."""

    csv_path = tmp_path / "mysql.csv"
    csv_path.write_text(
        "brand_id,brand_name,total_sessions,total_utterances,avg_utterances_per_session\n"
        " brand-a , Acme ,2000,30000,15.0\n",
        encoding="utf-8",
    )
    frame = dashboard.load_mysql_metrics(csv_path)
    assert list(frame.columns) == list(dashboard.MYSQL_REQUIRED_COLUMNS)
    assert frame.loc[0, "brand_id"] == "brand-a"
    assert frame.loc[0, "brand_name"] == "Acme"


def test_load_mysql_metrics_missing_column_raises(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """A MySQL CSV missing a required column should raise MissingColumnsError."""

    csv_path = tmp_path / "mysql_bad.csv"
    csv_path.write_text(
        "brand_id,brand_name,total_sessions\nbrand-a,Acme,1000\n",
        encoding="utf-8",
    )
    with pytest.raises(dashboard.MissingColumnsError):
        dashboard.load_mysql_metrics(csv_path)


def test_merge_metrics_inner_joins_on_brand_id(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """merge_metrics should inner-join two frames and keep only common brand ids."""

    pinot = dashboard.load_pinot_metrics(_write_pinot_csv(tmp_path / "pinot.csv"))
    mysql = dashboard.load_mysql_metrics(_write_mysql_csv(tmp_path / "mysql.csv"))
    merged = dashboard.merge_metrics(pinot, mysql)
    assert len(merged) == 3
    assert {"brand_id", "p95_ttfb_ms", "brand_name", "total_sessions"}.issubset(
        merged.columns
    )


def test_merge_metrics_raises_when_no_overlap(dashboard: ModuleType) -> None:
    """An empty intersection between Pinot and MySQL brand_ids should raise."""

    pinot = pd.DataFrame(
        {
            "brand_id": ["x"],
            "total_requests": [1],
            "avg_ttfb_ms": [1.0],
            "p50_ttfb_ms": [1.0],
            "p95_ttfb_ms": [1.0],
            "p99_ttfb_ms": [1.0],
        }
    )
    mysql = pd.DataFrame(
        {
            "brand_id": ["y"],
            "brand_name": ["Y"],
            "total_sessions": [1],
            "total_utterances": [1],
            "avg_utterances_per_session": [1.0],
        }
    )
    with pytest.raises(dashboard.DashboardError):
        dashboard.merge_metrics(pinot, mysql)


@pytest.mark.parametrize(
    ("p95", "p99", "expected"),
    [
        (3000, 6000, "GREEN"),
        (4999, 7999, "GREEN"),
        (6000, 9000, "YELLOW"),
        (5000, 8000, "YELLOW"),
        (8001, 14999, "RED"),
        (3000, 15001, "RED"),
        (12000, 20000, "RED"),
    ],
)
def test_classify_health_buckets(
    dashboard: ModuleType, p95: float, p99: float, expected: str
) -> None:
    """classify_health should bucket latencies according to spec thresholds."""

    assert dashboard.classify_health(p95, p99) == expected


def test_classify_health_handles_nan(dashboard: ModuleType) -> None:
    """NaN inputs should default to YELLOW so they get flagged for review."""

    assert dashboard.classify_health(float("nan"), 100) == "YELLOW"
    assert dashboard.classify_health(100, float("nan")) == "YELLOW"


def test_classify_health_honors_custom_thresholds(dashboard: ModuleType) -> None:
    """Callers should be able to tighten thresholds for stricter rollouts."""

    strict = dashboard.HealthThresholds(
        p95_green_max_ms=1000.0,
        p95_red_min_ms=2000.0,
        p99_green_max_ms=2000.0,
        p99_red_min_ms=3000.0,
    )
    assert dashboard.classify_health(900, 1500, thresholds=strict) == "GREEN"
    assert dashboard.classify_health(1500, 2500, thresholds=strict) == "YELLOW"
    assert dashboard.classify_health(2100, 2500, thresholds=strict) == "RED"
    assert dashboard.classify_health(1500, 3500, thresholds=strict) == "RED"


def test_add_health_status_appends_column(dashboard: ModuleType) -> None:
    """add_health_status should append a health_status column without mutating input."""

    frame = pd.DataFrame(
        {"p95_ttfb_ms": [3000, 6000, 9000], "p99_ttfb_ms": [6000, 9000, 20000]}
    )
    enriched = dashboard.add_health_status(frame)
    assert "health_status" not in frame.columns
    assert list(enriched["health_status"]) == ["GREEN", "YELLOW", "RED"]


def test_add_health_status_requires_latency_columns(dashboard: ModuleType) -> None:
    """A frame lacking p95/p99 columns should raise MissingColumnsError."""

    with pytest.raises(dashboard.MissingColumnsError):
        dashboard.add_health_status(pd.DataFrame({"foo": [1]}))


def test_build_summary_table_sorts_by_p95_desc(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """The summary table should expose required columns sorted by p95 desc."""

    pinot = dashboard.load_pinot_metrics(_write_pinot_csv(tmp_path / "pinot.csv"))
    mysql = dashboard.load_mysql_metrics(_write_mysql_csv(tmp_path / "mysql.csv"))
    enriched = dashboard.add_health_status(dashboard.merge_metrics(pinot, mysql))
    summary = dashboard.build_summary_table(enriched)
    assert list(summary.columns) == list(dashboard.SUMMARY_COLUMNS)
    p95_series = summary["p95_ttfb_ms"].tolist()
    assert p95_series == sorted(p95_series, reverse=True)


def test_compute_insights_picks_expected_brands(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """Insights should identify slowest, most-adopted, and healthiest brands."""

    pinot = dashboard.load_pinot_metrics(_write_pinot_csv(tmp_path / "pinot.csv"))
    mysql = dashboard.load_mysql_metrics(_write_mysql_csv(tmp_path / "mysql.csv"))
    enriched = dashboard.add_health_status(dashboard.merge_metrics(pinot, mysql))
    summary = dashboard.build_summary_table(enriched)
    insights = dashboard.compute_insights(summary)
    assert insights.slowest_brand == "Gamma"
    assert insights.most_adopted_brand == "Acme"
    assert insights.healthiest_brand == "Acme"
    assert insights.green_count + insights.yellow_count + insights.red_count == 3


def test_compute_insights_handles_empty_frame(dashboard: ModuleType) -> None:
    """Insights should not error on an empty frame; counts should be zero."""

    insights = dashboard.compute_insights(
        pd.DataFrame(
            columns=[
                "brand_name",
                "total_sessions",
                "p95_ttfb_ms",
                "p99_ttfb_ms",
                "health_status",
            ]
        )
    )
    assert insights.slowest_brand is None
    assert insights.green_count == 0
    assert insights.red_count == 0


def test_compute_insights_falls_back_when_no_green(dashboard: ModuleType) -> None:
    """Healthiest brand should fall back to lowest p95 when no GREEN brands exist."""

    frame = pd.DataFrame(
        {
            "brand_name": ["A", "B"],
            "total_sessions": [10, 20],
            "p95_ttfb_ms": [9000.0, 10000.0],
            "p99_ttfb_ms": [16000.0, 18000.0],
            "health_status": ["RED", "RED"],
        }
    )
    insights = dashboard.compute_insights(frame)
    assert insights.healthiest_brand == "A"


def test_format_insights_produces_readable_text(dashboard: ModuleType) -> None:
    """format_insights should produce a multi-line string with key labels."""

    insights = dashboard.RolloutInsights(
        slowest_brand="A",
        slowest_p95_ttfb_ms=9000.0,
        most_adopted_brand="B",
        most_adopted_total_sessions=1234,
        healthiest_brand="C",
        healthiest_p95_ttfb_ms=1500.0,
        green_count=1,
        yellow_count=2,
        red_count=3,
    )
    text = dashboard.format_insights(insights)
    assert "GPT Rollout Health Summary" in text
    assert "Slowest brand" in text
    assert "Most adopted" in text
    assert "Healthiest brand" in text


def test_generate_dashboard_writes_artifacts(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """The full pipeline should write merged_metrics.csv and dashboard.png."""

    pinot_path = _write_pinot_csv(tmp_path / "pinot.csv")
    mysql_path = _write_mysql_csv(tmp_path / "mysql.csv")
    output_dir = tmp_path / "out"
    artifacts, insights, summary = dashboard.generate_dashboard(
        pinot_csv=pinot_path,
        mysql_csv=mysql_path,
        output_dir=output_dir,
    )
    assert artifacts.merged_csv.exists()
    assert artifacts.dashboard_png.exists()
    assert artifacts.dashboard_png.stat().st_size > 0
    assert len(summary) == 3
    assert insights.slowest_brand == "Gamma"


def test_generate_dashboard_raises_on_no_overlap(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """The pipeline should fail fast when CSVs share no brand_ids."""

    pinot_path = tmp_path / "pinot.csv"
    pinot_path.write_text(
        "brand_id,total_requests,avg_ttfb_ms,p50_ttfb_ms,p95_ttfb_ms,p99_ttfb_ms\n"
        "brand-x,1,1,1,1,1\n",
        encoding="utf-8",
    )
    mysql_path = tmp_path / "mysql.csv"
    mysql_path.write_text(
        "brand_id,brand_name,total_sessions,total_utterances,avg_utterances_per_session\n"
        "brand-y,Y,1,1,1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(dashboard.DashboardError):
        dashboard.generate_dashboard(
            pinot_csv=pinot_path,
            mysql_csv=mysql_path,
            output_dir=tmp_path / "out",
        )


def test_main_cli_writes_files(
    dashboard: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI entry point should write artifacts and print an insight summary."""

    pinot_path = _write_pinot_csv(tmp_path / "pinot.csv")
    mysql_path = _write_mysql_csv(tmp_path / "mysql.csv")
    output_dir = tmp_path / "cli_out"
    exit_code = dashboard.main(
        [
            "--pinot-csv",
            str(pinot_path),
            "--mysql-csv",
            str(mysql_path),
            "--output-dir",
            str(output_dir),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert (output_dir / "merged_metrics.csv").exists()
    assert (output_dir / "dashboard.png").exists()
    assert "GPT Rollout Health Summary" in captured.out


def test_main_cli_returns_error_on_missing_input(
    dashboard: ModuleType, tmp_path: Path
) -> None:
    """The CLI should return a non-zero exit when a CSV is missing."""

    exit_code = dashboard.main(
        [
            "--pinot-csv",
            str(tmp_path / "nope.csv"),
            "--mysql-csv",
            str(tmp_path / "nope2.csv"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert exit_code == 2
