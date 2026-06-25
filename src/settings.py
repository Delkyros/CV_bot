"""Read tunable settings from environment variables (.env).

Everything adjustable should come from the environment so nothing operational
stays hardcoded. Each helper falls back to a documented default when the
variable is unset or malformed (logging a warning in the latter case), so the
pipeline still runs out of the box without a fully populated .env.

IMPORTANT: these helpers are meant to be called at RUNTIME (inside functions),
not at module-import time. main.py calls load_dotenv() only inside main(), after
the src modules are already imported, so reading env vars at import time would
miss everything defined in the .env file.
"""

import logging
import os

logger = logging.getLogger(__name__)


def env_str(name, default):
    """Return the env var as a stripped string, or `default` if unset/empty."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def env_float(name, default):
    """Return the env var as a float, or `default` if unset/empty/invalid."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s.", name, raw, default)
        return default


def env_int(name, default):
    """Return the env var as an int, or `default` if unset/empty/invalid."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid int for %s=%r; using default %s.", name, raw, default)
        return default


def env_list(name, default):
    """Return a comma-separated env var as a list of stripped items.

    `default` (any iterable) is returned as a list when the var is unset/empty.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]
