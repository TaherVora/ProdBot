"""
Pulls recent ERROR/CRITICAL logs for the target Cloud Run service.

POC note: this runs as a one-shot poll (call it on a cron / manually) rather
than a Pub/Sub push subscriber. Swap this module out for a push endpoint
later without touching anything downstream.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from google.cloud import logging as cloud_logging

import config

# https://cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry#LogSeverity
_SEVERITY_ORDER = {
    "DEFAULT": 0, "DEBUG": 100, "INFO": 200, "NOTICE": 300,
    "WARNING": 400, "ERROR": 500, "CRITICAL": 600, "ALERT": 700, "EMERGENCY": 800,
}


def _extract_fields(entry) -> dict:
    """
    Cloud Run log entries vary a lot in shape depending on whether the app
    logs structured JSON or plain text. Pull what we can, fall back gracefully.
    """
    payload = entry.payload
    message = None
    filename = None
    line = None

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg") or str(payload)
        filename = payload.get("filename")
        line = payload.get("line")
    else:
        message = str(payload)

    status = None
    endpoint = None
    if entry.http_request:
        status = entry.http_request.get("status")
        endpoint = entry.http_request.get("requestUrl") or entry.http_request.get("requestMethod")

    error_type = str(status) if status else entry.severity
    service_name = (entry.resource.labels or {}).get("service_name") if entry.resource else None

    return {
        "raw_log": message,
        "error_type": error_type,
        "endpoint": endpoint,
        "filename": filename,
        "line": line,
        "service_name": service_name,
        "severity": entry.severity,
        "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
    }


def fetch_recent_errors(minutes: int = None) -> list[dict]:
    minutes = minutes if minutes is not None else config.LOG_LOOKBACK_MINUTES
    client = cloud_logging.Client(project=config.GCP_PROJECT_ID)

    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    log_filter = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{config.CLOUD_RUN_SERVICE}" '
        f'severity>=ERROR '
        f'timestamp>="{since}"'
    )

    entries = client.list_entries(filter_=log_filter, order_by=cloud_logging.DESCENDING)
    return [_extract_fields(e) for e in entries]


def parse_pubsub_log_entry(entry: dict, min_severity: str = "WARNING") -> dict | None:
    """
    Same job as _extract_fields, but for a raw LogEntry as delivered by a
    Cloud Logging sink -> Pub/Sub push (plain JSON/dict), rather than an
    Entry object from the google-cloud-logging client library.

    Two shapes show up in practice:
      - application log: jsonPayload with a concise `message` plus the
        `filename`/`line` the app reported (used directly as Tier 1 in
        integrations/github.py — no stack-trace parsing needed), and a
        sibling httpRequest block (status, method, url) for the request
        that triggered it.
      - GCP request log: no payload at all, just an httpRequest block.

    Returns None if the entry's severity is below `min_severity` (defense in
    depth — the Pub/Sub subscription/sink filter is expected to already
    restrict to severity >= WARNING) or if there's nothing usable to extract.
    """
    severity = entry.get("severity", "DEFAULT")
    if _SEVERITY_ORDER.get(severity, 0) < _SEVERITY_ORDER.get(min_severity, 400):
        return None

    resource = entry.get("resource") or {}
    service_name = (resource.get("labels") or {}).get("service_name")

    http_request = entry.get("httpRequest") or {}
    status = http_request.get("status")

    text_payload = entry.get("textPayload")
    json_payload = entry.get("jsonPayload")

    raw_log = None
    filename = None
    line = None

    if text_payload:
        raw_log = text_payload.strip()
    elif isinstance(json_payload, dict):
        raw_log = json_payload.get("message") or json_payload.get("msg") or json.dumps(json_payload)
        filename = json_payload.get("filename")
        line = json_payload.get("line")
    elif http_request:
        # GCP-generated request log with no application payload at all.
        method = http_request.get("requestMethod")
        url = http_request.get("requestUrl")
        raw_log = " ".join(str(p) for p in (f"HTTP {status}" if status else None, method, url) if p)

    if not raw_log:
        return None

    endpoint = None
    if http_request:
        url = http_request.get("requestUrl")
        endpoint = urlparse(url).path if url else http_request.get("requestMethod")

    error_type = str(status) if status else severity

    return {
        "raw_log": raw_log,
        "error_type": error_type,
        "endpoint": endpoint,
        "filename": filename,
        "line": line,
        "service_name": service_name,
    }