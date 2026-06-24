import logging
import sys


def setup_logging(level=logging.INFO, log_file=None):
    """
    Configure the pipeline logging: a console handler (stdout) and, optionally, a
    file. Idempotent — repeated calls do not duplicate handlers.

    Each module should get its logger with `logging.getLogger(__name__)`; handler
    configuration is centralized here and triggered once at the entry point
    (main.py).
    """
    root = logging.getLogger()
    root.setLevel(level)

    if getattr(setup_logging, "_configured", False):
        # Allow readjusting the level on re-runs without recreating handlers.
        root.setLevel(level)
        return root

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    setup_logging._configured = True
    return root
