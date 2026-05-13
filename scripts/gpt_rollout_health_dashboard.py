#!/usr/bin/env python3
"""Generate a GPT rollout health dashboard from Pinot and MySQL CSV exports."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import matplotlib

# Force a non-interactive backend so the script works in headless / CI runs.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use)
import pandas as pd  # noqa: E402


LOGGER: Final[logging.Logger] = logging.getLogger("gpt_rollout_health_dashboard")

PINOT_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "brand_id",
    "total_requests",
    "avg_ttfb_ms",
    "p50_ttfb_ms",
    "p95_ttfb_ms",
    "p99_ttfb_ms",
)
MYSQL_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "brand_id",
    "brand_name",
    "total_sessions",
    "total_utterances",
    "avg_utterances_per_session",
)
SUMMARY_COLUMNS: Final[tuple[str, ...]] = (
    "brand_name",
    "total_sessions",
    "avg_utterances_per_session",
    "total_requests",
    "p50_ttfb_ms",
    "p95_ttfb_ms",
    "p99_ttfb_ms",
    "health_status",
)

HEALTH_GREEN: Final[str] = "GREEN"
HEALTH_YELLOW: Final[str] = "YELLOW"
HEALTH_RED: Final[str] = "RED"
HEALTH_COLOR_MAP: Final[dict[str, str]] = {
    HEALTH_GREEN: "#2ecc71",
    HEALTH_YELLOW: "#f1c40f",
    HEALTH_RED: "#e74c3c",
}


class DashboardError(RuntimeError):
    """Raise when the rollout health dashboard cannot be produced."""


class MissingColumnsError(DashboardError):
    """Raise when a required column is missing from an input CSV."""


@dataclass(frozen=True)
class HealthThresholds:
    """Bundle the latency thresholds (in ms) used to compute health status."""

    p95_green_max_ms: float = 5000.0
    p95_red_min_ms: float = 8000.0
    p99_green_max_ms: float = 8000.0
    p99_red_min_ms: float = 15000.0


@dataclass(frozen=True)
class DashboardArtifacts:
    """Hold paths to artifacts produced by the dashboard pipeline."""

    merged_csv: Path
    dashboard_png: Path


@dataclass(frozen=True)
class RolloutInsights:
    """Capture quick insights about the rollout snapshot."""

    slowest_brand: str | None
    slowest_p95_ttfb_ms: float | None
    most_adopted_brand: str | None
    most_adopted_total_sessions: int | None
    healthiest_brand: str | None
    healthiest_p95_ttfb_ms: float | None
    green_count: int
    yellow_count: int
    red_count: int


def _validate_columns(
    frame: pd.DataFrame, required: tuple[str, ...], source: str
) -> None:
    """Raise MissingColumnsError when a required column is absent from a frame."""

    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise MissingColumnsError(
            f"{source} is missing required columns: {', '.join(missing)}"
        )


def load_pinot_metrics(csv_path: Path) -> pd.DataFrame:
    """Load a Pinot latency metrics CSV and validate the schema."""

    if not csv_path.exists():
        raise DashboardError(f"Pinot CSV not found: {csv_path}")

    # Read everything as-is then coerce types so malformed cells surface clearly.
    frame = pd.read_csv(csv_path)
    _validate_columns(frame, PINOT_REQUIRED_COLUMNS, source=str(csv_path))

    numeric_columns = [c for c in PINOT_REQUIRED_COLUMNS if c != "brand_id"]
    frame[list(numeric_columns)] = frame[list(numeric_columns)].apply(
        pd.to_numeric, errors="coerce"
    )
    frame["brand_id"] = frame["brand_id"].astype(str).str.strip()
    return frame[list(PINOT_REQUIRED_COLUMNS)]


def load_mysql_metrics(csv_path: Path) -> pd.DataFrame:
    """Load a MySQL adoption metrics CSV and validate the schema."""

    if not csv_path.exists():
        raise DashboardError(f"MySQL CSV not found: {csv_path}")

    frame = pd.read_csv(csv_path)
    _validate_columns(frame, MYSQL_REQUIRED_COLUMNS, source=str(csv_path))

    numeric_columns = ("total_sessions", "total_utterances", "avg_utterances_per_session")
    frame[list(numeric_columns)] = frame[list(numeric_columns)].apply(
        pd.to_numeric, errors="coerce"
    )
    frame["brand_id"] = frame["brand_id"].astype(str).str.strip()
    frame["brand_name"] = frame["brand_name"].astype(str).str.strip()
    return frame[list(MYSQL_REQUIRED_COLUMNS)]


def merge_metrics(pinot: pd.DataFrame, mysql: pd.DataFrame) -> pd.DataFrame:
    """Inner-join Pinot latency and MySQL adoption metrics on brand_id."""

    if pinot.empty or mysql.empty:
        # Surface this early so callers don't silently produce empty dashboards.
        LOGGER.warning(
            "One of the input frames is empty (pinot=%d rows, mysql=%d rows).",
            len(pinot),
            len(mysql),
        )

    merged = pinot.merge(mysql, on="brand_id", how="inner", validate="one_to_one")
    if merged.empty:
        raise DashboardError(
            "No matching brand_id values between the Pinot and MySQL CSV inputs."
        )
    return merged


def classify_health(
    p95_ttfb_ms: float, p99_ttfb_ms: float, thresholds: HealthThresholds | None = None
) -> str:
    """Return GREEN, YELLOW, or RED for a single brand's latency snapshot."""

    config = thresholds or HealthThresholds()

    # NaNs cannot be classified safely; surface them as YELLOW to flag for review.
    if pd.isna(p95_ttfb_ms) or pd.isna(p99_ttfb_ms):
        return HEALTH_YELLOW

    # RED dominates so a single bad metric never gets hidden by other thresholds.
    if p95_ttfb_ms > config.p95_red_min_ms or p99_ttfb_ms > config.p99_red_min_ms:
        return HEALTH_RED

    # GREEN requires BOTH p95 and p99 to stay inside the healthy window.
    if p95_ttfb_ms < config.p95_green_max_ms and p99_ttfb_ms < config.p99_green_max_ms:
        return HEALTH_GREEN

    # Everything else (typical "warning" zone) falls back to YELLOW.
    return HEALTH_YELLOW


def add_health_status(
    frame: pd.DataFrame, thresholds: HealthThresholds | None = None
) -> pd.DataFrame:
    """Return a copy of the merged frame with a health_status column appended."""

    if "p95_ttfb_ms" not in frame.columns or "p99_ttfb_ms" not in frame.columns:
        raise MissingColumnsError(
            "Frame must contain p95_ttfb_ms and p99_ttfb_ms to compute health."
        )

    result = frame.copy()
    result["health_status"] = result.apply(
        lambda row: classify_health(
            row["p95_ttfb_ms"], row["p99_ttfb_ms"], thresholds=thresholds
        ),
        axis=1,
    )
    return result


def build_summary_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Project the merged frame down to the dashboard summary columns sorted by p95."""

    _validate_columns(frame, SUMMARY_COLUMNS, source="merged frame")
    summary = frame[list(SUMMARY_COLUMNS)].copy()
    summary = summary.sort_values(
        by="p95_ttfb_ms", ascending=False, na_position="last", kind="mergesort"
    )
    return summary.reset_index(drop=True)


def compute_insights(frame: pd.DataFrame) -> RolloutInsights:
    """Compute slowest, most-adopted, and healthiest brand insights."""

    if frame.empty:
        return RolloutInsights(
            slowest_brand=None,
            slowest_p95_ttfb_ms=None,
            most_adopted_brand=None,
            most_adopted_total_sessions=None,
            healthiest_brand=None,
            healthiest_p95_ttfb_ms=None,
            green_count=0,
            yellow_count=0,
            red_count=0,
        )

    # Slowest: highest p95 latency (NaNs ignored).
    slowest_row = frame.loc[frame["p95_ttfb_ms"].idxmax()] if frame["p95_ttfb_ms"].notna().any() else None
    # Most adopted: highest total_sessions.
    most_adopted_row = (
        frame.loc[frame["total_sessions"].idxmax()]
        if frame["total_sessions"].notna().any()
        else None
    )
    # Healthiest: prefer GREEN brands; among them pick lowest p95.
    green_only = frame[frame["health_status"] == HEALTH_GREEN]
    healthiest_pool = green_only if not green_only.empty else frame
    healthiest_row = (
        healthiest_pool.loc[healthiest_pool["p95_ttfb_ms"].idxmin()]
        if healthiest_pool["p95_ttfb_ms"].notna().any()
        else None
    )

    counts = frame["health_status"].value_counts()
    return RolloutInsights(
        slowest_brand=None if slowest_row is None else str(slowest_row["brand_name"]),
        slowest_p95_ttfb_ms=None if slowest_row is None else float(slowest_row["p95_ttfb_ms"]),
        most_adopted_brand=None if most_adopted_row is None else str(most_adopted_row["brand_name"]),
        most_adopted_total_sessions=None
        if most_adopted_row is None
        else int(most_adopted_row["total_sessions"]),
        healthiest_brand=None if healthiest_row is None else str(healthiest_row["brand_name"]),
        healthiest_p95_ttfb_ms=None if healthiest_row is None else float(healthiest_row["p95_ttfb_ms"]),
        green_count=int(counts.get(HEALTH_GREEN, 0)),
        yellow_count=int(counts.get(HEALTH_YELLOW, 0)),
        red_count=int(counts.get(HEALTH_RED, 0)),
    )


def format_insights(insights: RolloutInsights) -> str:
    """Return a human-readable, multi-line summary of rollout insights."""

    def _fmt_ms(value: float | None) -> str:
        """Format a millisecond value or em-dash when missing."""

        return f"{value:,.0f} ms" if value is not None else "—"

    lines = [
        "GPT Rollout Health Summary",
        f"  Health mix:        GREEN={insights.green_count}  "
        f"YELLOW={insights.yellow_count}  RED={insights.red_count}",
        f"  Slowest brand:     {insights.slowest_brand or '—'} "
        f"(p95 {_fmt_ms(insights.slowest_p95_ttfb_ms)})",
        f"  Most adopted:      {insights.most_adopted_brand or '—'} "
        f"({insights.most_adopted_total_sessions or 0:,} sessions)",
        f"  Healthiest brand:  {insights.healthiest_brand or '—'} "
        f"(p95 {_fmt_ms(insights.healthiest_p95_ttfb_ms)})",
    ]
    return "\n".join(lines)


def _bar_colors_for(statuses: pd.Series) -> list[str]:
    """Map a series of health statuses to bar colors."""

    return [HEALTH_COLOR_MAP.get(str(status), "#7f8c8d") for status in statuses]


def plot_dashboard(summary: pd.DataFrame, output_path: Path) -> Path:
    """Render the 3-panel rollout dashboard PNG and return its path."""

    if summary.empty:
        raise DashboardError("Cannot plot a dashboard from an empty summary frame.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bar_colors = _bar_colors_for(summary["health_status"])

    # 2x2 grid: p95 bars, sessions bars, scatter (spans bottom row).
    fig = plt.figure(figsize=(16, 10))
    grid = fig.add_gridspec(2, 2, hspace=0.45, wspace=0.25)
    ax_p95 = fig.add_subplot(grid[0, 0])
    ax_sessions = fig.add_subplot(grid[0, 1])
    ax_scatter = fig.add_subplot(grid[1, :])

    # p95 latency bars colored by health status.
    ax_p95.bar(summary["brand_name"], summary["p95_ttfb_ms"], color=bar_colors)
    ax_p95.set_title("p95 TTFB (ms) by brand")
    ax_p95.set_xlabel("Brand")
    ax_p95.set_ylabel("p95 TTFB (ms)")
    ax_p95.tick_params(axis="x", rotation=45)
    for label in ax_p95.get_xticklabels():
        label.set_horizontalalignment("right")

    # Total sessions bars (adoption signal) using the same color scheme.
    ax_sessions.bar(summary["brand_name"], summary["total_sessions"], color=bar_colors)
    ax_sessions.set_title("Total sessions by brand")
    ax_sessions.set_xlabel("Brand")
    ax_sessions.set_ylabel("Total sessions")
    ax_sessions.tick_params(axis="x", rotation=45)
    for label in ax_sessions.get_xticklabels():
        label.set_horizontalalignment("right")

    # Adoption vs latency scatter with per-point brand labels.
    ax_scatter.scatter(
        summary["total_sessions"],
        summary["p95_ttfb_ms"],
        c=bar_colors,
        s=120,
        edgecolors="black",
        linewidths=0.6,
    )
    for _, row in summary.iterrows():
        ax_scatter.annotate(
            str(row["brand_name"]),
            xy=(row["total_sessions"], row["p95_ttfb_ms"]),
            xytext=(6, 6),
            textcoords="offset points",
            fontsize=9,
        )
    ax_scatter.set_title("Adoption vs latency (sessions vs p95 TTFB)")
    ax_scatter.set_xlabel("Total sessions")
    ax_scatter.set_ylabel("p95 TTFB (ms)")
    ax_scatter.grid(True, linestyle="--", alpha=0.4)

    # Manual legend so the three health colors are explained once for the figure.
    legend_handles = [
        plt.Rectangle((0, 0), 1, 1, color=HEALTH_COLOR_MAP[HEALTH_GREEN], label="GREEN"),
        plt.Rectangle((0, 0), 1, 1, color=HEALTH_COLOR_MAP[HEALTH_YELLOW], label="YELLOW"),
        plt.Rectangle((0, 0), 1, 1, color=HEALTH_COLOR_MAP[HEALTH_RED], label="RED"),
    ]
    fig.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.99),
        ncol=3,
        frameon=False,
    )
    fig.suptitle("GPT Rollout Health Dashboard", fontsize=16, fontweight="bold", y=0.94)

    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def export_merged_csv(summary: pd.DataFrame, output_path: Path) -> Path:
    """Write the merged summary frame to disk as CSV and return its path."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_path, index=False)
    return output_path


def generate_dashboard(
    pinot_csv: Path,
    mysql_csv: Path,
    output_dir: Path,
    thresholds: HealthThresholds | None = None,
) -> tuple[DashboardArtifacts, RolloutInsights, pd.DataFrame]:
    """Run the full dashboard pipeline and return artifacts, insights, and summary."""

    pinot_frame = load_pinot_metrics(pinot_csv)
    mysql_frame = load_mysql_metrics(mysql_csv)
    merged = merge_metrics(pinot_frame, mysql_frame)
    enriched = add_health_status(merged, thresholds=thresholds)
    summary = build_summary_table(enriched)
    insights = compute_insights(summary)

    merged_csv_path = export_merged_csv(summary, output_dir / "merged_metrics.csv")
    dashboard_png_path = plot_dashboard(summary, output_dir / "dashboard.png")
    return (
        DashboardArtifacts(merged_csv=merged_csv_path, dashboard_png=dashboard_png_path),
        insights,
        summary,
    )


def _default_output_dir() -> Path:
    """Return the default reports directory stamped with today's UTC date."""

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return Path("reports") / "gpt_rollout" / today


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the rollout health dashboard."""

    parser = argparse.ArgumentParser(
        description=(
            "Generate a GPT rollout health dashboard from Pinot latency and "
            "MySQL adoption CSV exports."
        ),
    )
    parser.add_argument(
        "--pinot-csv",
        type=Path,
        required=True,
        help="Path to the Pinot latency metrics CSV.",
    )
    parser.add_argument(
        "--mysql-csv",
        type=Path,
        required=True,
        help="Path to the MySQL adoption metrics CSV.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for merged_metrics.csv and dashboard.png (default: reports/gpt_rollout/<date>).",
    )
    parser.add_argument(
        "--p95-green-max-ms",
        type=float,
        default=HealthThresholds().p95_green_max_ms,
        help="Upper bound (exclusive) of p95 TTFB for GREEN health.",
    )
    parser.add_argument(
        "--p95-red-min-ms",
        type=float,
        default=HealthThresholds().p95_red_min_ms,
        help="Lower bound (exclusive) of p95 TTFB that triggers RED health.",
    )
    parser.add_argument(
        "--p99-green-max-ms",
        type=float,
        default=HealthThresholds().p99_green_max_ms,
        help="Upper bound (exclusive) of p99 TTFB required for GREEN health.",
    )
    parser.add_argument(
        "--p99-red-min-ms",
        type=float,
        default=HealthThresholds().p99_red_min_ms,
        help="Lower bound (exclusive) of p99 TTFB that triggers RED health.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    """Initialize root logging configuration for the CLI entry point."""

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the GPT rollout health dashboard."""

    args = parse_args(argv)
    _configure_logging(args.verbose)

    thresholds = HealthThresholds(
        p95_green_max_ms=args.p95_green_max_ms,
        p95_red_min_ms=args.p95_red_min_ms,
        p99_green_max_ms=args.p99_green_max_ms,
        p99_red_min_ms=args.p99_red_min_ms,
    )
    output_dir = args.output_dir or _default_output_dir()

    try:
        artifacts, insights, _summary = generate_dashboard(
            pinot_csv=args.pinot_csv,
            mysql_csv=args.mysql_csv,
            output_dir=output_dir,
            thresholds=thresholds,
        )
    except DashboardError as exc:
        LOGGER.error("Dashboard generation failed: %s", exc)
        return 2

    print(format_insights(insights))
    print(f"\nMerged CSV:   {artifacts.merged_csv}")
    print(f"Dashboard:    {artifacts.dashboard_png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
