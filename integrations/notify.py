"""
Best-effort email notification fired at the end of pipeline.process_log(), for
every tier (duplicate/adapted/new). Never raises — a broken mail server or bad
SMTP creds must not affect error processing itself.
"""

import logging
import smtplib
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)


def _format_email(result: dict, log_data: dict) -> tuple[str, str]:
    service_name = log_data.get("service_name") or "unknown"
    status = result.get("status")

    source_files = result.get("source_files")
    files_block = "\n".join(f"  - {f}" for f in source_files) if source_files else "  none"

    subject = f"[ProdBot] {status} error — {service_name}"
    body = (
        f"Service:      {service_name}\n"
        f"Status:       {status}\n"
        f"Error log:    {log_data.get('raw_log')}\n"
        f"Error code:   {log_data.get('error_type') or 'n/a'}\n"
        f"File:         {log_data.get('filename') or 'n/a'}\n"
        f"Line:         {log_data.get('line') or 'n/a'}\n"
        f"Files searched by GitHub:\n{files_block}\n"
        f"\n"
        f"Suggested solution:\n{result.get('solution')}\n"
    )
    return subject, body


def _send_email(subject: str, body: str) -> None:
    if not config.SMTP_HOST or not config.NOTIFY_EMAIL_TO:
        log.info("notify: SMTP_HOST/NOTIFY_EMAIL_TO not configured, skipping email")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM_ADDRESS or config.SMTP_USERNAME
    message["To"] = ", ".join(config.NOTIFY_EMAIL_TO)
    message.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        if config.SMTP_USERNAME:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(message)
    log.info("notify: email sent to %s", config.NOTIFY_EMAIL_TO)


def notify(result: dict, log_data: dict) -> None:
    try:
        subject, body = _format_email(result, log_data)
        _send_email(subject, body)
    except Exception:
        log.exception("notify: failed to send email notification")
