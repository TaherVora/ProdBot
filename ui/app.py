"""
Streamlit demo for the error bot POC.

Three tabs:
  - Submit an error: paste a raw GCP Cloud Logging LogEntry JSON payload
    (the same shape the Pub/Sub push ingest service receives — see
    integrations.gcp.parse_pubsub_log_entry), run it through the real
    pipeline (embed -> dedup check -> reuse | GitHub retrieval -> Claude),
    see exactly what the bot decided and why.
  - History: everything stored in Postgres so far, most recent first.
  - Analysis: KPIs and charts over everything stored in Postgres.

Run with: streamlit run ui/app.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import analytics
import pipeline
from db import store as db
from integrations import gcp

st.set_page_config(page_title="Error Bot — Demo", page_icon="🛠️", layout="wide")

EXAMPLE_PAYLOAD = """{
  "httpRequest": {
    "requestMethod": "GET",
    "requestUrl": "https://cricket-fever-68175716613.us-central1.run.app/debug/error",
    "status": 404
  },
  "insertId": "6a8a3139000437e84a3b1c91",
  "jsonPayload": {
    "filename": "RequestLoggingFilter.java",
    "thread": "http-nio-8080-exec-4",
    "logger": "io.javabrains.ipldashboard.config.RequestLoggingFilter",
    "line": 42,
    "message": "GET /debug -> 404 (2 ms)"
  },
  "resource": {
    "type": "cloud_run_revision",
    "labels": {
      "revision_name": "cricket-fever-00005-f6q",
      "location": "us-central1",
      "service_name": "cricket-fever",
      "configuration_name": "cricket-fever",
      "project_id": "infoservices-hackathon-26"
    }
  },
  "timestamp": "2026-08-22T23:31:05.277Z",
  "severity": "WARNING",
  "labels": {
    "instanceId": "00a41e8c1dae99cfdbff8e0f10485e41e6b2e67292ddf5123967f2342483ce3b9996eb1cbff414c963e5b557ac926328e320396d5d12352f8596c7db11ca2d0131d4c9502a1e76285354b0e4c0baf1"
  },
  "logName": "projects/infoservices-hackathon-26/logs/run.googleapis.com%2Fstdout",
  "receiveTimestamp": "2026-08-22T23:31:05.375012680Z"
}"""


st.title("🛠️ Error Bot — Demo")
st.caption("Error triage agent (POC)")

tab_submit, tab_history, tab_analysis = st.tabs(["Submit an error", "History", "Analysis"])

# ---------------------------------------------------------------- Submit ---
with tab_submit:
    st.subheader("Paste a GCP log entry")
    st.caption(
        "Same JSON shape as a Cloud Logging LogEntry / Pub/Sub push payload — "
        "httpRequest, jsonPayload, resource, severity, etc."
    )
    with st.expander("Example payload"):
        st.code(EXAMPLE_PAYLOAD, language="json")

    payload_text = st.text_area("Log entry JSON", height=320, placeholder=EXAMPLE_PAYLOAD)

    submitted = st.button("Run through the bot", type="primary", disabled=not payload_text.strip())

    if submitted:
        log = None
        try:
            entry = json.loads(payload_text)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
        else:
            try:
                log = gcp.parse_pubsub_log_entry(entry)
            except Exception as e:
                st.error(f"Could not parse log entry: {e}")
            else:
                if log is None:
                    st.warning(
                        "Nothing usable extracted from this entry — either its severity is "
                        "below WARNING, or it has no message/httpRequest to work with."
                    )

        result = None
        if log:
            with st.spinner("Embedding, checking for duplicates, and diagnosing…"):
                try:
                    result = pipeline.process_log(log)
                except Exception as e:
                    st.error(f"Pipeline failed: {e}")
                    result = None

        if result and result["status"] == "duplicate":
            st.info(
                f"**Duplicate detected** — matched existing row `{result['matched_id']}` "
                f"(cosine distance `{result['distance']:.4f}`). "
                f"Now seen **{result['occurrence_count']}×**. Chat model was *not* called."
            )
            if result.get("source_files"):
                files = ", ".join(f"`{f}`" for f in result["source_files"])
                st.markdown(f"**Code context (from original diagnosis):** {files}")
            st.markdown("**Reused solution:**")
            st.write(result["solution"])

        elif result and result["status"] == "adapted":
            st.warning(
                f"**Adapted from a similar past error** — row `{result['id']}` inserted, "
                f"adapted from row `{result['reference_id']}` (cosine distance "
                f"`{result['distance']:.4f}`)."
            )
            if result.get("source_file"):
                st.markdown(f"**Code context:** 🟢 grounded — `{result['source_file']}`")
            else:
                st.markdown("**Code context:** 🔴 ungrounded — treat this as an unverified suggestion")
            st.markdown("**Adapted solution:**")
            st.write(result["solution"])

        elif result and result["status"] == "new":
            st.success(f"**New error** — stored as row `{result['id']}`")
            if not result.get("repo"):
                st.warning(f"No `service_repo_map` entry for `{log.get('service_name')}` — no code context was available.")
            if result["source_files"]:
                files = ", ".join(f"`{f}`" for f in result["source_files"])
                st.markdown(f"**Code context:** 🟢 grounded — {files} in `{result['repo']}`")
            else:
                st.markdown("**Code context:** 🔴 ungrounded")
            st.markdown("**Suggested solution:**")
            st.write(result["solution"])

# --------------------------------------------------------------- History ---
with tab_history:
    st.subheader("Stored errors")
    if st.button("Refresh"):
        st.rerun()

    try:
        rows = db.list_errors()
    except Exception as e:
        rows = []
        st.error(f"Could not load history: {e}")

    if not rows:
        st.caption("No errors stored yet — submit one in the first tab.")
    else:
        for row in rows:
            title = (
                f"#{row['id']} · {row['service_name'] or '?'} · {row['error_type'] or '?'} · "
                f"seen {row['occurrence_count']}× · [{row['resolution_tier']}]"
            )
            with st.expander(title):
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**Repo:** {row['repo'] or '—'}")
                reported = row['filename'] or '—'
                if row['filename'] and row.get('line'):
                    reported = f"{row['filename']}:{row['line']}"
                c2.markdown(f"**Reported at:** {reported}")
                c3.markdown(f"**Source file:** {row['source_file'] or '—'}")
                if row.get("source_files") and len(row["source_files"]) > 1:
                    others = ", ".join(f"`{f}`" for f in row["source_files"])
                    st.markdown(f"**All files considered:** {others}")
                if row.get("reference_error_id"):
                    st.caption(f"Adapted from row {row['reference_error_id']}")
                st.markdown("**Raw log:**")
                st.code(row["raw_log"])
                st.markdown("**Suggested solution:**")
                st.write(row["suggested_solution"])
                st.caption(f"First seen {row['first_seen']} · Last seen {row['last_seen']}")

# -------------------------------------------------------------- Analysis ---
with tab_analysis:
    st.subheader("Error analytics")
    if st.button("Refresh", key="refresh_analysis"):
        st.rerun()

    try:
        rows = db.list_errors(limit=100_000)
    except Exception as e:
        rows = []
        st.error(f"Could not load analytics: {e}")

    if not rows:
        st.caption("No errors stored yet — nothing to analyze.")
    else:
        df = pd.DataFrame(rows)
        kpis = analytics.build_kpis(df)

        k1, k2, k3 = st.columns(3)
        k1.metric("Number of errors", kpis["total_errors"],
                   help="Total occurrences, including repeats of an already-seen error.")
        k2.metric("Repeating errors", kpis["repeating_errors"],
                   help="Distinct errors that have been seen more than once.")
        if kpis["top_code"]:
            code, count = kpis["top_code"]
            k3.metric("Most common error code", code, help=f"{count} occurrence(s)")
        else:
            k3.metric("Most common error code", "—")

        st.markdown("##### Errors over time, by service")
        for service_name_ in sorted(df["service_name"].dropna().unique()):
            st.markdown(f"**{service_name_}**")
            service_df = df[df["service_name"] == service_name_]
            st.plotly_chart(analytics.service_time_chart(service_df), use_container_width=True)

        st.markdown("##### Fix quality: grounded vs. ungrounded")
        st.plotly_chart(analytics.grounding_chart(df), use_container_width=True)