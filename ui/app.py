"""
Streamlit demo for the error bot POC.

Three tabs:
  - Submit an error: paste/edit a log by hand, run it through the real
    pipeline (embed -> dedup check -> reuse | GitHub retrieval -> Claude),
    see exactly what the bot decided and why.
  - History: everything stored in Postgres so far, most recent first.
  - Analysis: KPIs and charts over everything stored in Postgres.

Run with: streamlit run ui/app.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import analytics
import pipeline
from db import store as db

st.set_page_config(page_title="Error Bot — Demo", page_icon="🛠️", layout="wide")

SAMPLE_LOGS = {
    "Container failed Cloud Run port binding": {
        "raw_log": (
            "Cloud Run: Container failed to start and listen on the port defined by the PORT "
            "environment variable within the allocated timeout. Default STARTUP TCP probe failed "
            "1 time consecutively for container \"beer-service-1\" on port 8080. Reason: connection "
            "refused. Application is bound to port 8082 (server.port=8082 in application.properties), "
            "not $PORT (8080), which Cloud Run requires."
        ),
        "error_type": "startup-timeout",
        "endpoint": "",
        "service_name": "beer-service",
        "filename": None,
        "line": None,
    },
    "In-memory H2 data lost / inconsistent across instances": {
        "raw_log": (
            "java.lang.RuntimeException at BeerServiceImpl.getById - beer not found after autoscale "
            "event. Cause: H2 is an in-memory DB re-seeded from data.sql on every cold start; Cloud "
            "Run's stateless, multi-instance/scale-to-zero model means a beer created via one "
            "instance doesn't exist on another, and NotFoundException has no @ExceptionHandler "
            "mapping so it surfaces as an unhandled 500."
        ),
        "error_type": "500",
        "endpoint": "/api/v1/beer/{beerId}",
        "service_name": "beer-service",
        "filename": "BeerServiceImpl.java",
        "line": 21,
    },
    "Unhandled NotFoundException returns 500 instead of 404": {
        "raw_log": (
            "java.lang.RuntimeException: com.taher.beerservice.web.controller.NotFoundException "
            "(no message). Cause: NotFoundException extends RuntimeException with no @ResponseStatus "
            "and no @ExceptionHandler in MvcExceptionHandler.java (which only catches "
            "ConstraintViolationException), so Spring's default resolver falls back to 500 instead "
            "of the intended 404."
        ),
        "error_type": "500",
        "endpoint": "/api/v1/beer/026cc3c8-3a0c-4083-a05b-e908048c1b08",
        "service_name": "beer-service",
        "filename": "BeerServiceImpl.java",
        "line": 21,
    },
    "POST /api/v1/beer 404 due to strict trailing-slash matching (Spring Boot 3)": {
        "raw_log": (
            "404 No endpoint POST /api/v1/beer. org.springframework.web.servlet.NoHandlerFoundException: "
            "No handler found for POST /api/v1/beer. Cause: BeerController.java declares "
            "@PostMapping(\"/\") (matches only /api/v1/beer/). Spring Framework 6 / Boot 3.0.0-M3 "
            "(see pom.xml) disabled default trailing-slash matching, so clients or a GCP load "
            "balancer / API Gateway route that strips the trailing slash now get a 404."
        ),
        "error_type": "404",
        "endpoint": "/api/v1/beer",
        "service_name": "beer-service",
        "filename": "BeerController.java",
        "line": None,
    },
    "Malformed UUID path variable causes 400": {
        "raw_log": (
            "Failed to convert value of type 'java.lang.String' to required type 'java.util.UUID'; "
            "nested exception is java.lang.IllegalArgumentException: Invalid UUID string: "
            "not-a-real-uuid. Cause: no custom binder/validation on the {beerId} path variable; any "
            "unsanitized path segment passed through by an upstream GCP proxy triggers this."
        ),
        "error_type": "400",
        "endpoint": "/api/v1/beer/not-a-real-uuid",
        "service_name": "beer-service",
        "filename": "BeerController.java",
        "line": 21,
    },
    "Duplicate UPC violates unique constraint, surfaces as unhandled 500": {
        "raw_log": (
            "org.springframework.dao.DataIntegrityViolationException: could not execute statement "
            "[Unique index or primary key violation: \"UK_BEER_UPC ON PUBLIC.BEER(UPC) VALUES "
            "( /* 1 */ '0631234200036' )\"]. Cause: client POSTed a beer with upc='0631234200036', "
            "which already exists (seeded by data.sql for 'Mango Bobs'). Beer.java declares "
            "@Column(unique = true) on upc, but nothing catches the resulting "
            "DataIntegrityViolationException, so it falls through to a generic 500 instead of a 409 "
            "Conflict."
        ),
        "error_type": "500",
        "endpoint": "/api/v1/beer/",
        "service_name": "beer-service",
        "filename": "BeerServiceImpl.java",
        "line": 27,
    },

}


st.title("🛠️ Error Bot — Demo")
st.caption("Error triage agent (POC)")

tab_submit, tab_history, tab_analysis = st.tabs(["Submit an error", "History", "Analysis"])

# ---------------------------------------------------------------- Submit ---
with tab_submit:
    st.subheader("Load a sample or paste your own")

    sample_choice = st.selectbox(
        "Sample logs", ["(none — write my own)"] + list(SAMPLE_LOGS.keys())
    )
    sample = SAMPLE_LOGS.get(sample_choice, {})

    service_name = st.text_input(
        "Cloud Run service name", value=sample.get("service_name", "amaz-clone"),
        help="Must match a row in service_repo_map, or code retrieval falls back to no context."
    )

    col1, col2 = st.columns(2)
    with col1:
        error_type = st.text_input("Error type / status code", value=sample.get("error_type", ""))
    with col2:
        endpoint = st.text_input("Endpoint / service", value=sample.get("endpoint", ""))

    raw_log = st.text_area("Raw log message", value=sample.get("raw_log", ""), height=80)

    col3, col4 = st.columns(2)
    with col3:
        filename = st.text_input(
            "Filename (optional)", value=sample.get("filename") or "",
            help="jsonPayload.filename — used as Tier 1 for GitHub code retrieval.",
        )
    with col4:
        line = st.number_input(
            "Line (optional)", value=sample.get("line") or 0, min_value=0, step=1,
            help="jsonPayload.line — passed to the agent as the reported failure location.",
        )

    submitted = st.button("Run through the bot", type="primary", disabled=not raw_log.strip())

    if submitted:
        log = {
            "raw_log": raw_log.strip(),
            "error_type": error_type.strip() or None,
            "endpoint": endpoint.strip() or None,
            "filename": filename.strip() or None,
            "line": int(line) or None,
            "service_name": service_name.strip() or None,
        }
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
                f"Now seen **{result['occurrence_count']}×**. Claude was *not* called."
            )
            st.markdown("**Reused solution:**")
            st.write(result["solution"])

        elif result and result["status"] == "new":
            st.success(f"**New error** — stored as row `{result['id']}`")
            if not result.get("repo"):
                st.warning(f"No `service_repo_map` entry for `{service_name}` — no code context was available.")
            if result["source_file"]:
                st.markdown(f"**Code context:** 🟢 grounded — `{result['source_file']}` in `{result['repo']}`")
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
            title = f"#{row['id']} · {row['service_name'] or '?'} · {row['error_type'] or '?'} · seen {row['occurrence_count']}×"
            with st.expander(title):
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**Repo:** {row['repo'] or '—'}")
                reported = row['filename'] or '—'
                if row['filename'] and row.get('line'):
                    reported = f"{row['filename']}:{row['line']}"
                c2.markdown(f"**Reported at:** {reported}")
                c3.markdown(f"**Source file:** {row['source_file'] or '—'}")
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