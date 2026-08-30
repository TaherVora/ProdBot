---
name: gcp-structured-logging
description: Use when adding structured HTTP request/error logging to a service (Java, Python, TypeScript/Node.js, or similar) that ships logs to GCP Cloud Logging (Cloud Run, GKE, App Engine, etc.). Covers the flat-JSON-to-stdout contract, field names, severity mapping, the httpRequest special field, and the one-line-per-request convention, so logs from every service in a system stay consistent and machine-parseable.
---

# GCP structured logging contract

A service emits one flat JSON object per log line to stdout. GCP's Cloud
Logging agent (on Cloud Run / GKE / App Engine) reads that stream, lifts a
handful of **special top-level keys** into real `LogEntry` fields, and folds
everything else into `jsonPayload`. It also stamps on `insertId`, `resource`,
`logName`, `receiveTimestamp`, and its own `labels` (instance id, etc.) —
**none of that is app code's job.** A service only ever writes the flat JSON
line below; it never constructs the outer envelope.

## What gets lifted vs. what stays in jsonPayload

Verified against a real ingested GCP Cloud Logging entry:

| Key you emit | Lifted to a top-level `LogEntry` field? | Notes |
|---|---|---|
| `severity` | Yes | one of GCP's enum values (below) |
| `time` | Yes → becomes `timestamp` | RFC 3339 string |
| `httpRequest` | Yes → its own `LogEntry.httpRequest` | object with `requestMethod`/`requestUrl`/`status` |
| `message` | **No** — stays inside `jsonPayload` | Cloud Logging's UI still uses it as the one-line summary text, it's just not a separate top-level field the way the three above are |
| `logger`, `thread`, `filename`, `line` | No | ordinary custom fields, land in `jsonPayload` as-is |

So the JSON a service writes to stdout for a request-summary line looks like:

```json
{"time":"2026-08-29T20:53:46.074Z","severity":"WARNING","message":"GET https://api.example.com/users/42 -> 404 (7 ms)","logger":"com.example.RequestLoggingFilter","thread":"http-nio-8080-exec-3","filename":"RequestLoggingFilter.java","line":54,"httpRequest":{"requestMethod":"GET","requestUrl":"https://api.example.com/users/42","status":404}}
```

GCP then splits that into `severity`/`timestamp`/`httpRequest` as real fields
and `{filename, message, thread, logger, line}` as `jsonPayload`.

## Severity mapping (use verbatim)

GCP's enum: `DEFAULT, DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL, ALERT, EMERGENCY`.
Map native log levels onto it:

| Native level | `severity` |
|---|---|
| TRACE / DEBUG | `DEBUG` |
| INFO | `INFO` |
| WARN | `WARNING` |
| ERROR | `ERROR` |
| unmapped/unknown | `DEFAULT` |

**Per-request severity rule (3-tier, use this by default):**

| Status code | `severity` |
|---|---|
| `>= 500` | `ERROR` |
| `400–499` | `WARNING` |
| `< 400` | `INFO` |

If a downstream alerting system only watches `severity=ERROR` and needs to
catch client errors too, that's a deliberate per-system decision to promote
4xx to `ERROR` as well — don't default to it silently, since it means
warnings and hard failures become indistinguishable in the log stream.

An exception that escapes request handling entirely (not just a 4xx/5xx
response) is always logged at `ERROR` immediately, in addition to the
per-request summary line.

## The `httpRequest` field

Only attach `httpRequest` to the **one summary line per request** — not to
every log statement. Populate exactly:
- `requestMethod` — HTTP verb
- `requestUrl` — full URL including query string
- `status` — numeric response status

Do not add extra keys to it beyond what GCP documents (`latency`,
`responseSize`, etc. are also valid if you have them, but don't invent
non-GCP keys here — anything else belongs at the top level of the JSON line,
not inside `httpRequest`).

## The one-line-per-request invariant

Don't let a service emit one log line from the controller, another from a
global exception handler, another from a filter. Wrap the whole request in a
single filter/middleware:

1. Record start time.
2. Run the rest of the request.
3. In a `finally`, compute duration, build the message string in the fixed
   shape below, decide severity from the status-code rule, and log **one**
   line.
4. If a global/central exception handler translates an exception into a
   response (e.g. a 404), have it **stash the exception** somewhere
   request-scoped (attribute/context var) instead of logging its own line —
   the filter/middleware picks it up and appends it to its one summary line.
5. Also catch-and-log (then rethrow) anything that escapes the filter chain
   itself, so nothing is silently swallowed.

**Message shape — keep this exact format across every service:**

```
{METHOD} {URL} -> {STATUS} ({DURATION_MS} ms)
```

e.g. `GET https://api.example.com/users/42 -> 404 (7 ms)`. Keeping this
identical across services is what lets any downstream log processing
(dashboards, alerting, dedup tooling) rely on a single parse rule instead of
one per service.

## Field reference to replicate in any language

Every log line: `time`, `severity`, `message`, `logger`, `thread`.
When caller/source info is available: `filename`, `line`.
Request-summary lines only: `httpRequest`.

- `logger` — fully-qualified logger/module name (Java: logger name; Python:
  `record.name`/module `__name__`; Node: the module/file emitting it).
- `thread` — thread name in Java; in single-threaded-event-loop languages
  (Node) use something meaningful like a request id, or omit if genuinely
  nothing maps to it — don't fabricate a constant value.
- `filename`/`line` — source location of the log call. Java gets this free
  from `event.getCallerData()`; Python from `LogRecord.filename`/`lineno`;
  Node needs an `Error().stack` parse or a logging library that exposes it
  (e.g. pino with `pino-caller`) — acceptable to omit if the library can't
  give it cheaply, but include it if it can, since a downstream tool may use
  `filename` to jump straight to the source of an error.

## Per-language recipes

### Java

Use a custom logback `Layout` that serializes each event to the flat JSON
shape, and SLF4J `MDC` to carry request context from a servlet filter into
the layout (MDC is thread-bound, which lines up with one-thread-per-request
servlet containers):

```java
package com.example.logging;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

import com.fasterxml.jackson.databind.ObjectMapper;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.ThrowableProxyUtil;
import ch.qos.logback.core.LayoutBase;

public class GcpJsonLayout extends LayoutBase<ILoggingEvent> {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Override
    public String doLayout(ILoggingEvent event) {
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("time", Instant.ofEpochMilli(event.getTimeStamp()).toString());
        fields.put("severity", toGcpSeverity(event.getLevel()));
        fields.put("message", messageWithStackTrace(event));
        fields.put("logger", event.getLoggerName());
        fields.put("thread", event.getThreadName());

        StackTraceElement[] callerData = event.getCallerData();
        if (callerData != null && callerData.length > 0) {
            fields.put("filename", callerData[0].getFileName());
            fields.put("line", callerData[0].getLineNumber());
        }

        Map<String, String> mdc = event.getMDCPropertyMap();
        String requestMethod = mdc.get("http.requestMethod");
        String requestUrl = mdc.get("http.requestUrl");
        String status = mdc.get("http.status");
        if (requestMethod != null && requestUrl != null && status != null) {
            Map<String, Object> httpRequest = new LinkedHashMap<>();
            httpRequest.put("requestMethod", requestMethod);
            httpRequest.put("requestUrl", requestUrl);
            httpRequest.put("status", Integer.valueOf(status));
            fields.put("httpRequest", httpRequest);
        }

        try {
            return MAPPER.writeValueAsString(fields) + System.lineSeparator();
        } catch (Exception e) {
            return "{\"severity\":\"ERROR\",\"message\":\"Failed to serialize log event\"}"
                    + System.lineSeparator();
        }
    }

    private String messageWithStackTrace(ILoggingEvent event) {
        String message = event.getFormattedMessage();
        IThrowableProxy throwableProxy = event.getThrowableProxy();
        if (throwableProxy != null) {
            message = message + "\n" + ThrowableProxyUtil.asString(throwableProxy);
        }
        return message;
    }

    private String toGcpSeverity(Level level) {
        switch (level.toInt()) {
            case Level.ERROR_INT: return "ERROR";
            case Level.WARN_INT: return "WARNING";
            case Level.INFO_INT: return "INFO";
            case Level.DEBUG_INT:
            case Level.TRACE_INT: return "DEBUG";
            default: return "DEFAULT";
        }
    }
}
```

Wire it up in `logback-spring.xml` (or `logback.xml`):

```xml
<configuration>
    <appender name="CONSOLE_JSON" class="ch.qos.logback.core.ConsoleAppender">
        <encoder class="ch.qos.logback.core.encoder.LayoutWrappingEncoder">
            <layout class="com.example.logging.GcpJsonLayout"/>
        </encoder>
    </appender>

    <root level="INFO">
        <appender-ref ref="CONSOLE_JSON"/>
    </root>
</configuration>
```

Servlet filter for the one-line-per-request part:

```java
@Component
public class RequestLoggingFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(RequestLoggingFilter.class);

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        long start = System.currentTimeMillis();
        try {
            chain.doFilter(request, response);
        } catch (Exception ex) {
            log.error("Unhandled exception while processing {} {}", request.getMethod(), request.getRequestURI(), ex);
            throw ex;
        } finally {
            long durationMs = System.currentTimeMillis() - start;
            int status = response.getStatus();
            String url = request.getRequestURL().toString();

            MDC.put("http.requestMethod", request.getMethod());
            MDC.put("http.requestUrl", url);
            MDC.put("http.status", String.valueOf(status));
            try {
                String message = String.format("%s %s -> %d (%d ms)", request.getMethod(), url, status, durationMs);
                if (status >= 500) {
                    log.error(message);
                } else if (status >= 400) {
                    log.warn(message);
                } else {
                    log.info(message);
                }
            } finally {
                MDC.remove("http.requestMethod");
                MDC.remove("http.requestUrl");
                MDC.remove("http.status");
            }
        }
    }
}
```

A global `@ExceptionHandler`/`@ControllerAdvice` that translates an exception
into a response should stash it on a request attribute instead of logging
its own line, so `RequestLoggingFilter` can fold it into the one summary
line for that request.

### Python

No built-in MDC — use `contextvars` as the equivalent, and a
`logging.Formatter` in place of the custom `Layout`:

```python
import json, logging, sys, time, contextvars
from datetime import datetime, timezone

_http_ctx = contextvars.ContextVar("http_request_ctx", default=None)

_SEVERITY = {
    logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARNING",
    logging.ERROR: "ERROR", logging.CRITICAL: "CRITICAL",
}

class GcpJsonFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        fields = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "message": message,
            "logger": record.name,
            "thread": record.threadName,
            "filename": record.filename,
            "line": record.lineno,
        }
        http_ctx = _http_ctx.get()
        if http_ctx:
            fields["httpRequest"] = http_ctx
        return json.dumps(fields)

def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(GcpJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
```

Flask-style middleware for the one-line-per-request part:

```python
@app.before_request
def _start_timer():
    g.start_time = time.time()

@app.after_request
def _log_request(response):
    duration_ms = int((time.time() - g.start_time) * 1000)
    _http_ctx.set({
        "requestMethod": request.method,
        "requestUrl": request.url,
        "status": response.status_code,
    })
    message = f"{request.method} {request.url} -> {response.status_code} ({duration_ms} ms)"
    log = logging.getLogger(__name__)
    if response.status_code >= 500:
        log.error(message)
    elif response.status_code >= 400:
        log.warning(message)
    else:
        log.info(message)
    _http_ctx.set(None)
    return response
```

(FastAPI/Django: same shape, wired as ASGI middleware / `MiddlewareMixin`
instead of Flask hooks.)

### TypeScript / Node.js

Use `AsyncLocalStorage` as the MDC equivalent, and a JSON logger (pino
shown; plain `console.log(JSON.stringify(...))` also works if you don't want
a dependency):

```ts
import pino from "pino";
import { AsyncLocalStorage } from "node:async_hooks";

const httpContext = new AsyncLocalStorage<Record<string, unknown>>();

const SEVERITY: Record<string, string> = {
  trace: "DEBUG", debug: "DEBUG", info: "INFO",
  warn: "WARNING", error: "ERROR", fatal: "CRITICAL",
};

const logger = pino({
  timestamp: () => `,"time":"${new Date().toISOString()}"`,
  formatters: {
    level(label) {
      return { severity: SEVERITY[label] ?? "DEFAULT" };
    },
    log(object) {
      const httpRequest = httpContext.getStore();
      return httpRequest ? { ...object, httpRequest } : object;
    },
  },
});
```

Express middleware:

```ts
app.use((req, res, next) => {
  const start = Date.now();
  res.on("finish", () => {
    const durationMs = Date.now() - start;
    const httpRequest = {
      requestMethod: req.method,
      requestUrl: req.originalUrl,
      status: res.statusCode,
    };
    httpContext.run(httpRequest, () => {
      const message = `${req.method} ${req.originalUrl} -> ${res.statusCode} (${durationMs} ms)`;
      const level = res.statusCode >= 500 ? "error" : res.statusCode >= 400 ? "warn" : "info";
      logger[level](message);
    });
  });
  next();
});
```

### Any other language — general recipe

1. Write one flat JSON object per log line to stdout/stderr. Never build the
   nested `insertId`/`resource`/`labels`/`logName` envelope yourself.
2. Map the language's native log level to the GCP `severity` enum using the
   table above.
3. Use whatever the language's context-propagation primitive is
   (thread-local, contextvar, AsyncLocalStorage, goroutine-scoped context,
   etc.) to carry `requestMethod`/`requestUrl`/`status` from the
   request-wrapping middleware down into the logger.
4. Attach `httpRequest` only on the one per-request summary line.
5. Reuse the exact field names (`time`, `severity`, `message`, `logger`,
   `thread`, `filename`, `line`, `httpRequest`) and the message shape
   `{METHOD} {URL} -> {STATUS} ({DURATION} ms)` — consistency across services
   is the entire point, not a per-service style choice.

## Do NOT

- Do not set `insertId`, `resource`, `logName`, `receiveTimestamp`, or the
  system-assigned `labels` yourself — the Cloud Logging agent supplies these
  from environment/service metadata; anything you put there is ignored or
  conflicts.
- Do not attach `httpRequest` to non-request log lines (pure debug/info
  statements) — it pollutes Cloud Logging's dedicated HTTP request view.
- Do not invent severity strings outside GCP's documented enum.
- Do not let a global exception handler log its own line in addition to the
  request filter's summary line — stash-and-append, per the pattern above,
  to keep one line per request.

## Checklist for wiring a new service

1. Add a stdout JSON logger/formatter emitting `time`, `severity`, `message`,
   `logger`, `thread`, `filename`, `line` (+ `httpRequest` when applicable).
2. Add the severity mapper (table above) and the 3-tier per-request rule
   (`>=500 → ERROR`, `400–499 → WARNING`, `<400 → INFO`).
3. Add request-scoped context propagation for
   `requestMethod`/`requestUrl`/`status`.
4. Add one request-wrapping filter/middleware logging exactly one summary
   line per request, in the fixed message shape.
5. Route handled exceptions back through that one summary line instead of
   logging separately.
6. Verify by hitting an endpoint and confirming stdout shows one JSON line
   per request with `time`/`severity`/`httpRequest` present and the rest of
   the fields matching this schema.
