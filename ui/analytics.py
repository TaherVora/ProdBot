"""
Chart builders for the Analysis tab. Pure functions: DataFrame in, Plotly
figure (or plain dict) out — no Streamlit calls here, so these can be
unit-tested or reused outside the app.
"""

from __future__ import annotations

import re

import pandas as pd
import plotly.graph_objects as go

# Validated categorical/status palette (see dataviz skill).
BLUE = "#2a78d6"
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
MUTED = "#898781"

INK = "#0b0b0b"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"

_LAYOUT_DEFAULTS = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="system-ui, -apple-system, sans-serif", color=INK, size=13),
    margin=dict(l=10, r=10, t=10, b=10),
    hoverlabel=dict(bgcolor="white", font_size=12),
)

# error_type -> status class color. These are severity states, not arbitrary
# series identities, so they use the status palette (as does grounding_chart).
_CLASS_COLORS = {"2xx": GOOD, "3xx": BLUE, "4xx": WARNING, "5xx": CRITICAL, "other": MUTED}
_CLASS_ORDER = ["2xx", "3xx", "4xx", "5xx", "other"]
_STATUS_CODE = re.compile(r'^\d{3}$')


def _style_axes(fig, x_grid=False, y_grid=False):
    fig.update_xaxes(showgrid=x_grid, gridcolor=GRIDLINE, zeroline=False,
                      linecolor=GRIDLINE, tickfont=dict(color=MUTED_INK))
    fig.update_yaxes(showgrid=y_grid, gridcolor=GRIDLINE, zeroline=False,
                      linecolor=GRIDLINE, tickfont=dict(color=MUTED_INK))
    return fig


def _error_class(error_type: str | None) -> str:
    """"500" -> "5xx", "404" -> "4xx"; anything non-numeric (e.g. "startup-timeout") -> "other"."""
    if error_type and _STATUS_CODE.match(error_type):
        return f"{error_type[0]}xx"
    return "other"


def build_kpis(df: pd.DataFrame) -> dict:
    total_errors = int(df["occurrence_count"].sum())
    repeating_errors = int((df["occurrence_count"] > 1).sum())

    top_code = None
    if len(df):
        by_type = df.groupby(df["error_type"].fillna("(unknown)"))["occurrence_count"].sum()
        by_type = by_type.sort_values(ascending=False)
        if len(by_type):
            top_code = (by_type.index[0], int(by_type.iloc[0]))

    return {
        "total_errors": total_errors,
        "repeating_errors": repeating_errors,
        "top_code": top_code,
    }


def service_time_chart(df: pd.DataFrame) -> go.Figure:
    """
    Stacked bar for a SINGLE service (caller filters df to one service_name
    first): hourly buckets of `first_seen` on the x-axis, error count on the
    y-axis, stacked/colored by status class (2xx/3xx/4xx/5xx/other).

    Note: a repeat of an already-seen error only bumps `occurrence_count` and
    `last_seen` on the existing row (see db.store.find_similar_error) — there's no
    per-occurrence timestamp log in this schema — so each bar counts
    *newly first-seen* errors in that hour, not every raw occurrence.
    """
    d = df.copy()
    d["hour"] = pd.to_datetime(d["first_seen"]).dt.tz_localize(None).dt.floor("h")
    d["class"] = d["error_type"].apply(_error_class)

    pivot = d.pivot_table(index="hour", columns="class", values="id", aggfunc="count", fill_value=0)
    pivot = pivot.sort_index()

    fig = go.Figure()
    for cls in _CLASS_ORDER:
        if cls not in pivot.columns:
            continue
        fig.add_bar(
            x=pivot.index, y=pivot[cls], name=cls, marker_color=_CLASS_COLORS[cls],
            hovertemplate=f"%{{x}}<br>{cls}: %{{y}}<extra></extra>",
        )
    fig.update_layout(
        **_LAYOUT_DEFAULTS, barmode="stack", height=280, bargap=0.2,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return _style_axes(fig, y_grid=True)


def grounding_chart(df: pd.DataFrame) -> go.Figure:
    """
    Single 100%-stacked horizontal bar showing what fraction of fixes were
    grounded in real code (a source file was found) vs. ungrounded — a
    quality/status signal, so it uses the status palette rather than generic
    categorical hues.
    """
    is_grounded = df["source_file"].notna()
    total = len(df)
    segments = [
        (True, "Grounded", GOOD),
        (False, "Ungrounded", CRITICAL),
    ]

    fig = go.Figure()
    for key, label, color in segments:
        n = int((is_grounded == key).sum())
        if n == 0:
            continue
        pct = round(100 * n / total, 1) if total else 0
        fig.add_bar(
            y=["Fixes"], x=[n], name=f"{label} ({pct}%)", orientation="h",
            marker=dict(color=color, line=dict(color="rgba(255,255,255,0.6)", width=2)),
            text=f"{pct}%", textposition="inside", insidetextfont=dict(color="white"),
            hovertemplate=f"{label}<br>%{{x}} errors ({pct}%)<extra></extra>",
        )
    fig.update_layout(
        **_LAYOUT_DEFAULTS, barmode="stack", height=140, showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.1, x=0),
    )
    fig.update_yaxes(visible=False)
    fig.update_xaxes(visible=False)
    return fig
