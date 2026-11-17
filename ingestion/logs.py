"""
Structured logs: one JSON line per event in data/logs/ingestion.jsonl

Why JSON lines? Easy to search and filter later ("show all failed PDFs slower than 5s"),
and the same format works with CloudWatch / Datadog / ELK in production.
The console stays clean for the readable printout.
"""
import structlog

from indexing.pii import mask_log_event
from ingestion.config import settings

_configured_for = None


def get_logger():
    global _configured_for
    if _configured_for != settings.log_path:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.format_exc_info,     # a stack trace becomes text here, so the masker below sees it too
                mask_log_event,                           # personal data never reaches a log line (Phase 6)
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.WriteLoggerFactory(file=open(settings.log_path, "a", encoding="utf-8")),
            cache_logger_on_first_use=False,
        )
        _configured_for = settings.log_path
    return structlog.get_logger()
