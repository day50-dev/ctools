"""
log - Shared structured logging for ctools.

Configure once per CLI entry point. Import `get_logger` anywhere.

Usage:
    from ctools.log import configure_logging, get_logger
    configure_logging(verbose=True)
    log = get_logger()
    log.info("concepts_extracted", count=5, source="opencode/ses_abc")
"""

import logging
import os
import sys

import structlog


class _LazyStderrFactory:
    """PrintLoggerFactory that reads sys.stderr at call time, not init time.

    This avoids stale file handles when pytest captures/replaces stderr.
    """

    def __call__(self):
        return structlog.PrintLogger(file=sys.stderr)

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _add_level_filter(min_level: str):
    """Return a structlog processor that drops events below min_level."""
    threshold = _LEVELS.get(min_level, logging.WARNING)

    def filter_level(logger, method_name, event_dict):
        level = event_dict.get("level", "").upper()
        if _LEVELS.get(level, logging.WARNING) < threshold:
            raise structlog.DropEvent
        return event_dict

    return filter_level


def configure_logging(verbose: bool = False) -> None:
    """Configure structlog for the process.

    verbose=True  → DEBUG level, pretty console output
    verbose=False → WARNING level (quiet by default)
    LOGLEVEL env var overrides both.
    """
    level = os.environ.get("LOGLEVEL", "DEBUG" if verbose else "WARNING").upper()

    renderer = structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _add_level_filter(level),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=_LazyStderrFactory(),
        cache_logger_on_first_use=False,
    )

    log = get_logger()
    log.debug("logging_configured", level=level)


def get_logger(**kwargs):
    """Get a bound logger instance."""
    return structlog.stdlib.get_logger(**kwargs)
