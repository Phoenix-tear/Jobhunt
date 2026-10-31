"""Exception hierarchy shared by the HTTP client, adapters and runner.

The runner treats each kind differently for company health:

* NotFoundError / InvalidResponseError  -> counted as a failure (dead-slug logic)
* TransientFetchError                   -> recorded, but not counted as a dead slug
* AuthRequiredError                     -> the source itself is blocked; stop the source
"""

from __future__ import annotations


class JobHuntError(Exception):
    """Base class for all expected errors."""


class FetchError(JobHuntError):
    """A fetch for one company/board did not produce usable data."""

    kind = "fetch_error"


class NotFoundError(FetchError):
    """The board does not exist (HTTP 404, or a redirect away from the API)."""

    kind = "not_found"


class InvalidResponseError(FetchError):
    """The response was not the JSON shape we expect (HTML page, malformed JSON...)."""

    kind = "invalid_response"


class TransientFetchError(FetchError):
    """Timeout, network error, 429 or 5xx that survived all retries."""

    kind = "transient"


class AuthRequiredError(FetchError):
    """The endpoint now demands credentials (HTTP 401/403)."""

    kind = "auth_required"


class ConfigError(JobHuntError):
    """config.yaml is missing or malformed."""


class MigrationError(JobHuntError):
    """A migration failed, or an applied migration file was edited afterwards."""
