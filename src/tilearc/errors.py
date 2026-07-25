"""Exception hierarchy.

Every error the CLI is expected to surface derives from :class:`TilearcError`;
``cli.main`` prints those as a single clean line rather than a traceback.
"""

from __future__ import annotations


class TilearcError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class ConfigError(TilearcError):
    """A park config or version list is missing, unreadable, or malformed."""

    exit_code = 2


class CredentialsError(TilearcError):
    """TDR credentials are missing or incomplete."""

    exit_code = 3


class CredentialsExpiredError(CredentialsError):
    """TDR credentials are present but past their expiry (or rejected upstream).

    Raised *instead of* letting the job grind through a wall of 403s.
    """

    exit_code = 3


class QuotaError(TilearcError):
    """A job would exceed a configured tile cap or a shared proxy quota."""

    exit_code = 4


class JobMismatchError(TilearcError):
    """An existing state DB describes a different job than the one requested."""

    exit_code = 5


class VerifyError(TilearcError):
    """An archive failed integrity checks."""

    exit_code = 6
