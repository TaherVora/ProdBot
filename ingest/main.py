"""
Pub/Sub push receiver for cricket-fever error events.

The subscription is fed by a Cloud Logging sink filtered to severity >=
WARNING, so each Pub/Sub message's data is a raw LogEntry JSON blob (the same
shape Cloud Logging's API returns) rather than a custom payload — see
integrations.gcp.parse_pubsub_log_entry for the two shapes that show up
(an application jsonPayload log carrying message/filename/line, and a
GCP-generated httpRequest-only log).

Cloud Run + Pub/Sub push wiring (topic, subscription, invoker service
account) is set up separately, out of scope here. This service just needs
to exist at a stable URL with a POST route that:
  - decodes the Pub/Sub push envelope and the LogEntry inside it,
  - runs the extracted fields through the same pipeline.process_log() the
    Streamlit demo uses,
  - acks (2xx) even on a bad/filtered message, so one malformed or
    below-threshold entry doesn't wedge the subscription in a retry loop.
"""

import base64
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import Response

import pipeline
from integrations import gcp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest")

app = FastAPI()


@app.post("/pubsub/push")
async def pubsub_push(request: Request):
    envelope = await request.json()
    message = envelope.get("message", {})

    try:
        data = base64.b64decode(message.get("data", "")).decode("utf-8")
        entry = json.loads(data)
        log = gcp.parse_pubsub_log_entry(entry)
    except Exception:
        logger.exception("Dropping unparsable Pub/Sub message %s", message.get("messageId"))
        return Response(status_code=200)

    if log is None:
        logger.info("Skipping message %s: below severity threshold or nothing usable", message.get("messageId"))
        return Response(status_code=204)

    try:
        result = pipeline.process_log(log)
    except Exception:
        logger.exception("pipeline.process_log failed for message %s", message.get("messageId"))
        return Response(status_code=500)

    logger.info("Processed message %s: %s", message.get("messageId"), result["status"])
    return Response(status_code=204)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
