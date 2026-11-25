"""Play Store MCP Server - Main server implementation."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

import structlog
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from play_store_mcp.analytics_client import AnalyticsDataClient
from play_store_mcp.bigquery_client import BigQueryClient
from play_store_mcp.client import (
    DEFAULT_REGIONS_VERSION,
    PlayStoreClient,
    PlayStoreClientError,
)
from play_store_mcp.crashlytics_client import CrashlyticsClient
from play_store_mcp.reporting_client import ReportingClient, _parse_reporting_rows

# Configure structured logging to stderr (stdout is reserved for MCP JSON-RPC)
log_level = os.environ.get("PLAY_STORE_MCP_LOG_LEVEL", "INFO")
numeric_level = getattr(logging, log_level.upper(), logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        # show_locals=False is a security control, not a style choice.
        # RichTracebackFormatter defaults it to True, which renders every
        # frame's locals — and the credential-loading frames hold the parsed
        # service account dict, so a malformed key would print its own
        # private_key into stderr (and from there into Docker/k8s/IDE logs).
        structlog.dev.ConsoleRenderer(
            exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
        ),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
logger = structlog.get_logger(__name__)


def _require_credentials_object(parsed: Any, header: str) -> dict[str, Any]:
    """A credentials header must decode to a non-empty JSON object.

    A JSON string would otherwise reach the loader as a *server-side file path*
    (``"/etc/..."``), and a falsy value (``{}``, ``0``, ``[]``) would silently fall
    back to the server's own ambient credentials.
    """
    if not isinstance(parsed, dict) or not parsed:
        raise PlayStoreClientError(
            f"{header} header must be a non-empty JSON object (service account key)"
        )
    return parsed


def _credentials_from_request_headers() -> dict[str, Any] | None:
    """Parse per-request service account credentials from the request headers.

    Returns the parsed credentials, or None when the request carries none and
    the caller should fall back to the shared client.

    Every client resolver must consult this. When only some of them did, a
    request authenticated as one tenant silently executed against the server's
    own ambient credentials on the resolvers that ignored the headers.

    Raises:
        PlayStoreClientError: if a header is present but malformed.
    """
    headers = get_http_headers() or {}

    if "x-google-credentials" in headers:
        try:
            parsed = json.loads(headers["x-google-credentials"])
        except json.JSONDecodeError as e:
            raise PlayStoreClientError(f"Invalid JSON in X-Google-Credentials header: {e}") from e
        return _require_credentials_object(parsed, "X-Google-Credentials")

    if "x-google-credentials-base64" in headers:
        try:
            creds_bytes = base64.b64decode(headers["x-google-credentials-base64"])
            parsed = json.loads(creds_bytes.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PlayStoreClientError(
                f"Invalid base64 or JSON in X-Google-Credentials-Base64 header: {e}"
            ) from e
        return _require_credentials_object(parsed, "X-Google-Credentials-Base64")

    return None


def get_client_from_context() -> PlayStoreClient:
    """Resolve a PlayStoreClient for the current request.

    Per-request credentials in the X-Google-Credentials /
    X-Google-Credentials-Base64 headers take precedence; otherwise the shared
    client from the lifespan is used.

    Raises:
        PlayStoreClientError: if credentials are invalid or unavailable.
    """
    per_request = _credentials_from_request_headers()
    if per_request is not None:
        return PlayStoreClient(credentials_json=per_request)

    client: PlayStoreClient | None = _shared_state.get("client")
    if client is not None:
        return client

    raise PlayStoreClientError(
        "No credentials provided. Set X-Google-Credentials or X-Google-Credentials-Base64 header, "
        "or configure server with GOOGLE_PLAY_STORE_CREDENTIALS environment variable."
    )


def get_reporting_client_from_context() -> ReportingClient:
    """Resolve a ReportingClient (Android Vitals: crash/ANR) for the current request.

    Vitals require the Reporting API scope, which is separate from the
    Publisher API scope PlayStoreClient uses, so this is intentionally a
    distinct client/credential path rather than reusing get_client_from_context.
    """
    per_request = _credentials_from_request_headers()
    if per_request is not None:
        return ReportingClient(credentials_json=per_request)

    reporting_client: ReportingClient | None = _shared_state.get("reporting_client")
    if reporting_client is not None:
        return reporting_client

    reporting_client = ReportingClient()
    _shared_state["reporting_client"] = reporting_client
    return reporting_client


def get_crashlytics_client_from_context() -> CrashlyticsClient:
    """Resolve a Firebase Crashlytics client for the current request."""
    per_request = _credentials_from_request_headers()
    if per_request is not None:
        return CrashlyticsClient(credentials_json=per_request)

    crashlytics_client: CrashlyticsClient | None = _shared_state.get("crashlytics_client")
    if crashlytics_client is not None:
        return crashlytics_client

    crashlytics_client = CrashlyticsClient()
    _shared_state["crashlytics_client"] = crashlytics_client
    return crashlytics_client


def get_bigquery_client_from_context() -> BigQueryClient:
    """Resolve a BigQueryClient (raw event/crash/session data) for the current request.

    BigQuery requires the bigquery.readonly scope, separate from the Publisher
    and Reporting API scopes, so this is its own client/credential path.
    """
    per_request = _credentials_from_request_headers()
    if per_request is not None:
        return BigQueryClient(credentials_json=per_request)

    bigquery_client: BigQueryClient | None = _shared_state.get("bigquery_client")
    if bigquery_client is not None:
        return bigquery_client

    bigquery_client = BigQueryClient()
    _shared_state["bigquery_client"] = bigquery_client
    return bigquery_client


def get_analytics_client_from_context() -> AnalyticsDataClient:
    """Resolve an AnalyticsDataClient (GA4 aggregated reports) for the current request.

    Requires the analytics.readonly scope, separate from the other clients'
    scopes, so this is its own client/credential path.
    """
    per_request = _credentials_from_request_headers()
    if per_request is not None:
        return AnalyticsDataClient(credentials_json=per_request)

    analytics_client: AnalyticsDataClient | None = _shared_state.get("analytics_client")
    if analytics_client is not None:
        return analytics_client

    analytics_client = AnalyticsDataClient()
    _shared_state["analytics_client"] = analytics_client
    return analytics_client


class AppState(TypedDict):
    """Shared state threaded through the FastMCP lifespan context.

    A TypedDict, not a dataclass, so every existing subscript access
    (``_shared_state["client"]``, including the dict-style access FastMCP's
    yielded lifespan context and this module's own tests use) keeps working
    unchanged -- this only adds mypy key-name and value-type checking, no
    runtime behavior change.
    """

    client: PlayStoreClient | None
    credentials_updated: bool
    reporting_client: ReportingClient | None
    crashlytics_client: CrashlyticsClient | None
    bigquery_client: BigQueryClient | None
    analytics_client: AnalyticsDataClient | None


# Shared fallback client, used when a request carries no per-request
# credential header. Populated by the lifespan on startup and swapped by the
# /credentials route. Module-level so custom routes and get_client_from_context
# can reach it without depending on framework-internal context plumbing.
_shared_state: AppState = {
    "client": None,
    "credentials_updated": False,
    "reporting_client": None,
    "crashlytics_client": None,
    "bigquery_client": None,
    "analytics_client": None,
}

# Recommended minimum length for PLAY_STORE_MCP_ADMIN_TOKEN, below which
# _run_http logs a startup warning. `openssl rand -hex 32` (the documented
# recommendation) produces a 64-char token; this is a low bar, not a target.
_MIN_ADMIN_TOKEN_LENGTH = 16


@asynccontextmanager
async def lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
    """Initialize the shared PlayStoreClient on startup."""
    logger.info("Initializing Play Store MCP Server")
    try:
        client = PlayStoreClient()
        # Validate credentials off the event loop — _get_service() does blocking
        # discovery/auth, matching the offload used by the /credentials route.
        _ = await asyncio.to_thread(client._get_service)
        logger.info("Play Store client initialized successfully")
        _shared_state["client"] = client
    except PlayStoreClientError as e:
        logger.warning("Play Store client initialization failed", error=str(e))
        _shared_state["client"] = None

    yield _shared_state

    logger.info("Shutting down Play Store MCP Server")


def _validate_deploy_file(file_path: str) -> str | None:
    """Return error message if file_path is invalid, None if valid."""
    resolved = os.path.realpath(file_path)
    if not resolved.lower().endswith((".apk", ".aab")):
        return "file_path must be a .apk or .aab file"
    if not Path(resolved).is_file():
        return f"File not found: {resolved}"
    return None


_DEFAULT_BIGQUERY_MAX_BYTES_CAP = 10_000_000_000  # 10 GB


def _bigquery_max_bytes_cap() -> int:
    """Operator-set ceiling for bigquery_execute_query's max_bytes_billed.

    The per-call value is caller-controlled, so without a server-side ceiling the
    "cost guardrail" could be raised to any amount by the model.
    """
    raw = os.environ.get("PLAY_STORE_MCP_BIGQUERY_MAX_BYTES_BILLED", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_BIGQUERY_MAX_BYTES_CAP
    except ValueError:
        return _DEFAULT_BIGQUERY_MAX_BYTES_CAP


def _validate_rollout(pct: float) -> str | None:
    """Return error message if rollout percentage is invalid, None if valid."""
    # 0 would send an inProgress release with userFraction 0.0, which Play rejects.
    if not (0.0 < pct <= 100.0):
        return "rollout_percentage must be greater than 0.0 and at most 100.0"
    return None


def _env_read_only() -> bool:
    """Return True if PLAY_STORE_MCP_READ_ONLY is set to a truthy value."""
    return os.environ.get("PLAY_STORE_MCP_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# When True, all write/mutating tools are disabled. Initialized from the
# environment at import time; may be overridden by the --read-only CLI flag.
READ_ONLY: bool = _env_read_only()

READ_ONLY_ERROR = (
    "Server is running in read-only mode; write operations are disabled. "
    "Unset PLAY_STORE_MCP_READ_ONLY (or omit --read-only) to enable writes."
)


def set_read_only(value: bool) -> None:
    """Set the process-wide read-only flag."""
    global READ_ONLY
    READ_ONLY = value


def _read_only_block(operation: str) -> dict[str, Any] | None:
    """Return an error object if read-only mode blocks a write, else None."""
    if READ_ONLY:
        logger.warning("Blocked write operation in read-only mode", operation=operation)
        return {"error": f"{READ_ONLY_ERROR} (attempted: {operation})"}
    return None


def _upload_result(payload: dict[str, Any], *, commit: bool) -> dict[str, Any]:
    """Annotate an artifact upload result with what happened to the edit.

    Without this the caller cannot tell a published upload from a validation
    run: both return the same version code and hashes.
    """
    result = dict(payload)
    result["committed"] = commit
    if not commit:
        result["note"] = (
            "Validation only: Play accepted the artifact and the edit was discarded, "
            "so nothing was published and this version code is still available."
        )
    return result


# Per-tool hints for clients that gate approval on them (and for CodeMode, which
# otherwise collapses every tool — refunds and deletes included — into one opaque
# `execute`). A tool is a WRITE tool iff it honours read-only mode via
# _read_only_block; tests/test_read_only.py keeps that inventory honest.
_READ_TOOL = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE_TOOL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def _code_mode_enabled() -> bool:
    """Return True unless CODE_MODE explicitly opts out of the code-mode transform.

    Enabled by default; set CODE_MODE=0/false/no/off (case-insensitive) to opt
    out and fall back to the classic tool list.
    """
    return os.environ.get("CODE_MODE", "").strip().lower() not in {"0", "false", "no", "off"}


def _build_transforms() -> list[Any]:
    """Return the FastMCP transforms for this process.

    Default: the tool surface is wrapped in the experimental CodeMode transform
    (search/get_schema/execute meta-tools + sandboxed execution), which cuts
    per-request tool-list overhead. Set CODE_MODE=0 to opt out and expose the
    classic tool surface instead.
    """
    if not _code_mode_enabled():
        return []
    # Imported lazily so opting out (CODE_MODE=0) never touches fastmcp's
    # experimental module.
    from fastmcp.experimental.transforms.code_mode import (  # noqa: PLC0415
        CodeMode,
    )

    logger.info(
        "Exposing tools via the code-mode transform (search/get_schema/execute); "
        "set CODE_MODE=0 to opt out and use the classic tool list instead."
    )
    return [CodeMode()]


# Initialize the MCP server
mcp = FastMCP(
    "Play Store MCP Server",
    lifespan=lifespan,
    transforms=_build_transforms(),
)


# =============================================================================
# Publishing Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def deploy_app(
    package_name: str,
    track: str,
    file_path: str,
    release_notes: str | None = None,
    release_notes_language: str = "en-US",
    rollout_percentage: float = 100.0,
) -> dict[str, Any]:
    """Deploy an APK or AAB file to a Play Store track.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Release track - one of: internal, alpha, beta, production
        file_path: Absolute path to APK or AAB file
        release_notes: Optional release notes for this version (string for single language,
                      or use release_notes_multilang for multiple languages)
        release_notes_language: Language code for release notes (default: en-US)
        rollout_percentage: Rollout percentage (0-100). Default 100 for full rollout.

    Returns:
        Deployment result with success status and details
    """
    if blocked := _read_only_block("deploy_app"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    client = get_client_from_context()

    result = client.deploy_app(
        package_name=package_name,
        track=track,
        file_path=file_path,
        release_notes=release_notes,
        release_notes_language=release_notes_language,
        rollout_percentage=rollout_percentage,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def deploy_app_multilang(
    package_name: str,
    track: str,
    file_path: str,
    release_notes: dict[str, str],
    rollout_percentage: float = 100.0,
) -> dict[str, Any]:
    """Deploy an APK or AAB file with multi-language release notes.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Release track - one of: internal, alpha, beta, production
        file_path: Absolute path to APK or AAB file
        release_notes: Dictionary mapping language codes to release notes
                      (e.g., {"en-US": "Bug fixes", "es-ES": "Corrección de errores"})
        rollout_percentage: Rollout percentage (0-100). Default 100 for full rollout.

    Returns:
        Deployment result with success status and details
    """
    if blocked := _read_only_block("deploy_app_multilang"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    client = get_client_from_context()

    result = client.deploy_app(
        package_name=package_name,
        track=track,
        file_path=file_path,
        release_notes=release_notes,
        rollout_percentage=rollout_percentage,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def promote_release(
    package_name: str,
    from_track: str,
    to_track: str,
    version_code: int,
    rollout_percentage: float = 100.0,
) -> dict[str, Any]:
    """Promote a release from one track to another.

    Args:
        package_name: App package name
        from_track: Source track (internal, alpha, beta)
        to_track: Destination track (alpha, beta, production)
        version_code: Version code to promote
        rollout_percentage: Rollout percentage for target track (0-100)

    Returns:
        Promotion result with success status and details
    """
    if blocked := _read_only_block("promote_release"):
        return blocked
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    client = get_client_from_context()

    result = client.promote_release(
        package_name=package_name,
        from_track=from_track,
        to_track=to_track,
        version_code=version_code,
        rollout_percentage=rollout_percentage,
    )

    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def get_releases(package_name: str) -> list[dict[str, Any]]:
    """Get release status for all tracks of an app.

    Args:
        package_name: App package name

    Returns:
        List of tracks with their releases and version information
    """
    client = get_client_from_context()

    tracks = client.get_releases(package_name)
    return [track.model_dump() for track in tracks]


@mcp.tool(annotations=_WRITE_TOOL)
def halt_release(
    package_name: str,
    track: str,
    version_code: int,
) -> dict[str, Any]:
    """Halt a staged rollout.

    Use this to stop a release that is currently rolling out.
    The release will be marked as halted and users will stop receiving updates.

    Args:
        package_name: App package name
        track: Track containing the release (internal, alpha, beta, production)
        version_code: Version code of the release to halt

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("halt_release"):
        return blocked
    client = get_client_from_context()

    result = client.halt_release(
        package_name=package_name,
        track=track,
        version_code=version_code,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_rollout(
    package_name: str,
    track: str,
    version_code: int,
    rollout_percentage: float,
) -> dict[str, Any]:
    """Update the rollout percentage for a staged release.

    Use this to increase or decrease the percentage of users receiving an update.
    Set to 100 to complete the rollout.

    Args:
        package_name: App package name
        track: Track containing the release
        version_code: Version code of the staged release
        rollout_percentage: New rollout percentage (0-100)

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("update_rollout"):
        return blocked
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    client = get_client_from_context()

    result = client.update_rollout(
        package_name=package_name,
        track=track,
        version_code=version_code,
        rollout_percentage=rollout_percentage,
    )

    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def get_app_details(
    package_name: str,
    language: str = "en-US",
) -> dict[str, Any]:
    """Get app details including title, description, and developer info.

    Args:
        package_name: App package name
        language: Language code for localized content (default: en-US)

    Returns:
        App details including title, descriptions, and developer information
    """
    client = get_client_from_context()

    details = client.get_app_details(package_name, language)
    return details.model_dump()


# =============================================================================
# Reviews Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_reviews(
    package_name: str,
    max_results: int = 50,
    translation_language: str | None = None,
) -> list[dict[str, Any]]:
    """Get recent reviews for an app.

    Args:
        package_name: App package name
        max_results: Maximum number of reviews to return (default: 50, max: 100)
        translation_language: Optional language code to translate reviews to

    Returns:
        List of reviews with ratings, comments, and author info
    """
    client = get_client_from_context()

    reviews = client.get_reviews(
        package_name=package_name,
        max_results=min(max_results, 100),
        translation_language=translation_language,
    )

    return [review.model_dump() for review in reviews]


@mcp.tool(annotations=_WRITE_TOOL)
def reply_to_review(
    package_name: str,
    review_id: str,
    reply_text: str,
) -> dict[str, Any]:
    """Reply to a user review.

    Args:
        package_name: App package name
        review_id: ID of the review to reply to (from get_reviews)
        reply_text: Text of the reply (will be visible to the reviewer)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("reply_to_review"):
        return blocked
    client = get_client_from_context()

    result = client.reply_to_review(
        package_name=package_name,
        review_id=review_id,
        reply_text=reply_text,
    )

    return result.model_dump()


# =============================================================================
# Vitals Tools (crash / ANR — Play Developer Reporting API)
# =============================================================================
#
# Ported from AgiMaulana/GooglePlayConsoleMcp (MIT licensed), rewired onto
# ReportingClient/PlayStoreClientError instead of a bare AuthorizedSession.
# Requires the service account to have the "View app quality data"
# permission in Play Console (separate from the Publisher API's Release
# Manager role) and the Reporting API scope/enablement at the GCP project.


@mcp.tool(annotations=_READ_TOOL)
def get_crash_rate(
    package_name: str,
    days: int = 7,
    version_code: str = "",
) -> dict[str, Any]:
    """Fetch daily crash rate from Android Vitals.

    Returns crashRate, userPerceivedCrashRate, and distinctUsers by version
    code. Bad-behavior threshold: userPerceivedCrashRate > 1.09%.

    Args:
        package_name: App package name
        days: Past days to include (default 7, max 365)
        version_code: Optional version code filter
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.query_crash_rate(package_name, days, version_code or None)
    rows = _parse_reporting_rows(raw.get("rows", []))
    return {
        "packageName": package_name,
        "periodDays": days,
        "badBehaviorThreshold": {"userPerceivedCrashRate": 0.0109},
        "rows": rows,
    }


@mcp.tool(annotations=_READ_TOOL)
def get_anr_rate(
    package_name: str,
    days: int = 7,
    version_code: str = "",
) -> dict[str, Any]:
    """Fetch daily ANR (Application Not Responding) rate from Android Vitals.

    Returns anrRate, userPerceivedAnrRate, and distinctUsers by version code.
    Bad-behavior threshold: userPerceivedAnrRate > 0.47%.

    Args:
        package_name: App package name
        days: Past days to include (default 7, max 365)
        version_code: Optional version code filter
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.query_anr_rate(package_name, days, version_code or None)
    rows = _parse_reporting_rows(raw.get("rows", []))
    return {
        "packageName": package_name,
        "periodDays": days,
        "badBehaviorThreshold": {"userPerceivedAnrRate": 0.0047},
        "rows": rows,
    }


@mcp.tool(annotations=_READ_TOOL)
def get_wakelock_rate(
    package_name: str,
    days: int = 7,
    version_code: str = "",
) -> dict[str, Any]:
    """Fetch stuck background wake lock rate from Android Vitals.

    Args:
        package_name: App package name
        days: Past days to include (default 7, max 365)
        version_code: Optional version code filter
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.query_wakelock_rate(package_name, days, version_code or None)
    rows = _parse_reporting_rows(raw.get("rows", []))
    return {"packageName": package_name, "periodDays": days, "rows": rows}


@mcp.tool(annotations=_READ_TOOL)
def get_wakeup_rate(
    package_name: str,
    days: int = 7,
    version_code: str = "",
) -> dict[str, Any]:
    """Fetch excessive CPU wakeup rate from Android Vitals.

    Args:
        package_name: App package name
        days: Past days to include (default 7, max 365)
        version_code: Optional version code filter
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.query_wakeup_rate(package_name, days, version_code or None)
    rows = _parse_reporting_rows(raw.get("rows", []))
    return {"packageName": package_name, "periodDays": days, "rows": rows}


@mcp.tool(annotations=_READ_TOOL)
def get_vitals_summary(
    package_name: str,
    days: int = 7,
) -> dict[str, Any]:
    """Get combined Android Vitals: crash rate and ANR rate per version code.

    Returns per-version averages over the period with bad-behavior threshold
    flags (userPerceivedCrashRate > 1.09%, userPerceivedAnrRate > 0.47%).

    Args:
        package_name: App package name
        days: Past days to include (default 7, max 365)
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    crash_rows = _parse_reporting_rows(client.query_crash_rate(package_name, days).get("rows", []))
    anr_rows = _parse_reporting_rows(client.query_anr_rate(package_name, days).get("rows", []))

    def _aggregate(rows: list[dict[str, Any]], rate_key: str, perceived_key: str) -> dict[str, Any]:
        by_version: dict[str, Any] = {}
        for row in rows:
            vc = row.get("versionCode") or "unknown"
            entry = by_version.setdefault(vc, {"values": [], "perceived": [], "users": []})
            if isinstance(row.get(rate_key), (int, float)):
                entry["values"].append(row[rate_key])
            if isinstance(row.get(perceived_key), (int, float)):
                entry["perceived"].append(row[perceived_key])
            if isinstance(row.get("distinctUsers"), (int, float)):
                entry["users"].append(row["distinctUsers"])
        result = {}
        for vc, data in by_version.items():
            avg = lambda lst: round(sum(lst) / len(lst), 6) if lst else None  # noqa: E731
            result[vc] = {
                f"avg_{rate_key}": avg(data["values"]),
                f"avg_{perceived_key}": avg(data["perceived"]),
                "avgDistinctUsers": avg(data["users"]),
            }
        return result

    crash_by_vc = _aggregate(crash_rows, "crashRate", "userPerceivedCrashRate")
    anr_by_vc = _aggregate(anr_rows, "anrRate", "userPerceivedAnrRate")

    all_vcs = sorted(
        set(crash_by_vc) | set(anr_by_vc),
        key=lambda x: int(x) if str(x).isdigit() else 0,
        reverse=True,
    )
    summary = []
    for vc in all_vcs:
        entry: dict[str, Any] = {"versionCode": vc}
        entry.update(crash_by_vc.get(vc, {}))
        entry.update(anr_by_vc.get(vc, {}))
        crash_pct = entry.get("avg_userPerceivedCrashRate")
        anr_pct = entry.get("avg_userPerceivedAnrRate")
        entry["exceedsCrashThreshold"] = crash_pct is not None and crash_pct > 0.0109
        entry["exceedsAnrThreshold"] = anr_pct is not None and anr_pct > 0.0047
        summary.append(entry)

    return {
        "packageName": package_name,
        "periodDays": days,
        "badBehaviorThresholds": {"userPerceivedCrashRate": 0.0109, "userPerceivedAnrRate": 0.0047},
        "latestVersionSummary": summary[0] if summary else None,
        "allVersions": summary,
    }


@mcp.tool(annotations=_READ_TOOL)
def list_error_issues(
    package_name: str,
    days: int = 30,
    issue_type: str = "",
    max_results: int = 50,
) -> dict[str, Any]:
    """List crash/ANR/non-fatal error issues with reports in the last N days.

    The Reporting API has no "resolved/open" issue status — an issue is
    included here whenever it has >=1 error report inside the requested
    time window. Use days to bound what counts as "still occurring".

    Args:
        package_name: App package name
        days: Past days to include (default 30, max 365)
        issue_type: Optional filter: "CRASH", "ANR", or "NON_FATAL"
        max_results: Max issues to return (default 50, max 1000)
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.search_error_issues(
        package_name,
        days=days,
        issue_type=issue_type or None,
        page_size=min(max_results, 1000),
    )
    return {
        "packageName": package_name,
        "periodDays": days,
        "issues": raw.get("errorIssues", []),
        "totalIssues": len(raw.get("errorIssues", [])),
    }


@mcp.tool(annotations=_READ_TOOL)
def get_error_reports(
    package_name: str,
    issue_id: str = "",
    days: int = 30,
    issue_type: str = "",
    max_results: int = 20,
    max_report_text_chars: int = 4000,
) -> dict[str, Any]:
    """Get raw error reports with stack trace (reportText) for crash/ANR issues.

    Pass issue_id (the trailing id from an ErrorIssue's "name" field, as
    returned by list_error_issues) to fetch the reports behind one specific
    issue, including the device-produced stack trace / blocked-thread dump.

    A single ANR reportText can be 50-100K+ chars (a dump of every thread in
    the process, not just the blocked one) — reportText is truncated to
    max_report_text_chars by default (the blocked/crashing thread is always
    first). Pass 0 for the untruncated text.

    Args:
        package_name: App package name
        issue_id: Optional error issue id to scope to one issue (from list_error_issues)
        days: Past days to include (default 30, max 365)
        issue_type: Optional filter: "CRASH", "ANR", or "NON_FATAL"
        max_results: Max reports to return (default 20, max 100)
        max_report_text_chars: Truncate each reportText to this many chars (default 4000, 0 = no truncation)
    """
    days = max(1, min(days, 365))
    client = get_reporting_client_from_context()
    raw = client.search_error_reports(
        package_name,
        days=days,
        issue_id=issue_id or None,
        issue_type=issue_type or None,
        page_size=min(max_results, 100),
    )
    reports = raw.get("errorReports", [])
    if max_report_text_chars > 0:
        for report in reports:
            text = report.get("reportText") or ""
            if len(text) > max_report_text_chars:
                report["reportText"] = text[:max_report_text_chars] + (
                    f"\n... [truncated, {len(text) - max_report_text_chars} more chars; "
                    "raise max_report_text_chars to see full text]"
                )
    return {
        "packageName": package_name,
        "periodDays": days,
        "reports": reports,
        "totalReports": len(reports),
    }


@mcp.tool(annotations=_READ_TOOL)
def list_crashlytics_issues(
    project_id: str,
    app_id: str,
    days: int = 30,
    error_type: str = "",
    state: str = "",
    search: str = "",
    max_results: int = 25,
    page_token: str = "",
) -> dict[str, Any]:
    """List Firebase Crashlytics issues, with the issue IDs used to close them.

    This is the correct source of an issue_id for get_crashlytics_issue and
    close_crashlytics_issue. The IDs returned by list_error_issues come from
    Android Vitals (Play Developer Reporting API), which tracks the same crash
    under a different identifier — passing one of those to the Crashlytics
    tools fails with an opaque "Internal error encountered."

    Args:
        project_id: Firebase/Google Cloud project ID
        app_id: Firebase app ID (for example, 1:1234567890:android:abcdef)
        days: Past days of events to rank issues over (default 30, max 365)
        error_type: Optional filter: "FATAL", "NON_FATAL", or "ANR"
        state: Optional filter: "OPEN", "CLOSED", or "MUTED"
        search: Optional search over issue title and stack trace, as space
            separated prefix terms (for example, BadParcelableException
            MainTabsScreen); quote a term for an exact match
        max_results: Max issues to return (default 25, max 100)
        page_token: Page token from a previous call's nextPageToken

    Returns:
        Issues with issueId, title, subtitle, errorType, state, versions,
        event/user counts, and a Firebase console uri
    """
    client = get_crashlytics_client_from_context()
    return client.list_issues(
        project_id=project_id,
        app_id=app_id,
        days=days,
        error_type=error_type,
        state=state,
        search=search,
        page_size=max_results,
        page_token=page_token,
    )


@mcp.tool(annotations=_READ_TOOL)
def get_crashlytics_issue(
    project_id: str,
    app_id: str,
    issue_id: str,
) -> dict[str, Any]:
    """Get one Firebase Crashlytics issue, to confirm an ID before closing it.

    Args:
        project_id: Firebase/Google Cloud project ID
        app_id: Firebase app ID (for example, 1:1234567890:android:abcdef)
        issue_id: Full 32-character lowercase hex Crashlytics issue ID (from
            list_crashlytics_issues)

    Returns:
        The issue with its title, errorType, state, and Firebase console uri
    """
    client = get_crashlytics_client_from_context()
    return client.get_issue(
        project_id=project_id,
        app_id=app_id,
        issue_id=issue_id,
    )


@mcp.tool(annotations=_WRITE_TOOL)
def close_crashlytics_issue(
    project_id: str,
    app_id: str,
    issue_id: str,
) -> dict[str, Any]:
    """Close a Firebase Crashlytics issue, including fatal crashes and Android ANRs.

    This changes the issue state in Firebase Crashlytics to CLOSED. It does not
    close the similarly named Android Vitals issue in Google Play Console,
    because the public Play Developer Reporting API only supports searching
    those issues and exposes no state-update endpoint.

    Disabled in read-only mode.

    Args:
        project_id: Firebase/Google Cloud project ID
        app_id: Firebase app ID (for example, 1:1234567890:android:abcdef)
        issue_id: Full 32-character lowercase hex Crashlytics issue ID from
            list_crashlytics_issues (for example,
            c07d6e046632025ecd72f628ee1bf2ce), not the full resource name, not
            a truncated prefix, and not an Android Vitals issue ID from
            list_error_issues

    Returns:
        The updated Firebase Crashlytics issue with state CLOSED
    """
    if blocked := _read_only_block("close_crashlytics_issue"):
        return blocked
    client = get_crashlytics_client_from_context()
    return client.close_issue(
        project_id=project_id,
        app_id=app_id,
        issue_id=issue_id,
    )


@mcp.tool(annotations=_READ_TOOL)
def get_review(
    package_name: str,
    review_id: str,
    translation_language: str | None = None,
) -> dict[str, Any]:
    """Get a single user review by its ID.

    Args:
        package_name: App package name
        review_id: Review ID (from get_reviews)
        translation_language: Optional language to translate the review to

    Returns:
        The review with rating, text, and any developer reply
    """
    client = get_client_from_context()

    review = client.get_review(
        package_name=package_name,
        review_id=review_id,
        translation_language=translation_language,
    )

    return review.model_dump()


# =============================================================================
# Subscription Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_subscriptions(package_name: str) -> list[dict[str, Any]]:
    """List all subscription products for an app.

    Args:
        package_name: App package name

    Returns:
        List of subscription products with their base plans
    """
    client = get_client_from_context()

    subscriptions = client.list_subscriptions(package_name)
    return [sub.model_dump() for sub in subscriptions]


@mcp.tool(annotations=_READ_TOOL)
def get_subscription_status(
    package_name: str,
    subscription_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of a subscription purchase.

    Args:
        package_name: App package name
        subscription_id: Subscription product ID
        purchase_token: The purchase token from the client app

    Returns:
        Subscription purchase status including expiry and renewal info
    """
    client = get_client_from_context()

    status = client.get_subscription_purchase(
        package_name=package_name,
        subscription_id=subscription_id,
        token=purchase_token,
    )

    return status.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_voided_purchases(
    package_name: str,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """List voided purchases (refunds, chargebacks).

    Args:
        package_name: App package name
        max_results: Maximum number of results (default: 100)

    Returns:
        List of voided purchases with reason and timing
    """
    client = get_client_from_context()

    voided = client.list_voided_purchases(
        package_name=package_name,
        max_results=max_results,
    )

    return [v.model_dump() for v in voided]


@mcp.tool(annotations=_READ_TOOL)
def get_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of an in-app product purchase.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app

    Returns:
        Product purchase status (purchase/consumption/acknowledgement state, order, region)
    """
    client = get_client_from_context()

    purchase = client.get_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
    )

    return purchase.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def acknowledge_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
    developer_payload: str | None = None,
) -> dict[str, Any]:
    """Acknowledge an in-app product purchase.

    Purchases not acknowledged within 3 days are automatically refunded.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app
        developer_payload: Optional payload to associate with the purchase

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("acknowledge_product_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.acknowledge_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
        developer_payload=developer_payload,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def consume_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Consume an in-app product purchase (for consumable products).

    Marks the product as consumed so the user can purchase it again.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("consume_product_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.consume_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def refund_order(
    package_name: str,
    order_id: str,
    revoke: bool = False,
) -> dict[str, Any]:
    """Refund an order, optionally revoking the user's entitlement.

    Args:
        package_name: App package name
        order_id: Order ID to refund
        revoke: If True, also revoke the user's entitlement (default: False)

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("refund_order"):
        return blocked
    client = get_client_from_context()

    result = client.refund_order(package_name=package_name, order_id=order_id, revoke=revoke)

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def cancel_subscription_purchase(
    package_name: str,
    purchase_token: str,
    cancellation_type: str = "USER_REQUESTED_STOP_RENEWALS",
) -> dict[str, Any]:
    """Cancel a subscription purchase.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        cancellation_type: USER_REQUESTED_STOP_RENEWALS (default),
            DEVELOPER_REQUESTED_STOP_PAYMENTS, or CANCELLATION_TYPE_UNSPECIFIED

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("cancel_subscription_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_subscription_purchase(
        package_name=package_name,
        token=purchase_token,
        cancellation_type=cancellation_type,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def defer_subscription_purchase(
    package_name: str,
    purchase_token: str,
    defer_duration: str,
    etag: str,
) -> dict[str, Any]:
    """Defer a subscription purchase's next renewal.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        defer_duration: Duration to defer, e.g. "604800s" for 7 days
        etag: Current etag of the subscription purchase

    Returns:
        Result with success status and new expiry details
    """
    if blocked := _read_only_block("defer_subscription_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.defer_subscription_purchase(
        package_name=package_name,
        token=purchase_token,
        defer_duration=defer_duration,
        etag=etag,
    )

    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def revoke_subscription_purchase(
    package_name: str,
    purchase_token: str,
    refund_type: str = "full",
) -> dict[str, Any]:
    """Revoke (refund) a subscription purchase.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        refund_type: "full" or "prorated" (default: full)

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("revoke_subscription_purchase"):
        return blocked
    if refund_type not in ("full", "prorated"):
        return {"error": "refund_type must be 'full' or 'prorated'"}
    client = get_client_from_context()

    result = client.revoke_subscription_purchase(
        package_name=package_name,
        token=purchase_token,
        refund_type=refund_type,
    )

    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def get_product_purchase_v2(
    package_name: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of an in-app product purchase using the v2 API.

    Unlike get_product_purchase, this identifies the purchase by token alone
    (no product ID) and returns line items and acknowledgement state.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app

    Returns:
        Product purchase (v2) status
    """
    client = get_client_from_context()

    purchase = client.get_product_purchase_v2(package_name=package_name, token=purchase_token)

    return purchase.model_dump()


# =============================================================================
# In-App Products Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_in_app_products(package_name: str) -> list[dict[str, Any]]:
    """List all in-app products for an app.

    Args:
        package_name: App package name

    Returns:
        List of in-app products with SKU, title, description, and pricing
    """
    client = get_client_from_context()

    products = client.list_in_app_products(package_name)
    return [product.model_dump() for product in products]


@mcp.tool(annotations=_READ_TOOL)
def get_in_app_product(
    package_name: str,
    sku: str,
) -> dict[str, Any]:
    """Get details of a specific in-app product.

    Args:
        package_name: App package name
        sku: Product SKU identifier

    Returns:
        In-app product details including title, description, and pricing
    """
    client = get_client_from_context()

    product = client.get_in_app_product(package_name, sku)
    return product.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def create_in_app_product(
    package_name: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    """Create a new in-app product in the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product: In-app product resource body (e.g. sku, purchaseType, defaultPrice,
            listings, status, defaultLanguage)

    Returns:
        The created in-app product
    """
    if blocked := _read_only_block("create_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.create_in_app_product(package_name=package_name, product=product)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_in_app_product(
    package_name: str,
    sku: str,
    product: dict[str, Any],
    auto_convert_missing_prices: bool = False,
) -> dict[str, Any]:
    """Update (replace) an existing in-app product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier
        product: In-app product resource body
        auto_convert_missing_prices: Auto-convert prices for regions without a
            specified price based on the default price (default: False)

    Returns:
        The updated in-app product
    """
    if blocked := _read_only_block("update_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.update_in_app_product(
        package_name=package_name,
        sku=sku,
        product=product,
        auto_convert_missing_prices=auto_convert_missing_prices,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def patch_in_app_product(
    package_name: str,
    sku: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    """Partially update an existing in-app product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier
        product: Partial in-app product resource body with fields to change

    Returns:
        The patched in-app product
    """
    if blocked := _read_only_block("patch_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.patch_in_app_product(package_name=package_name, sku=sku, product=product)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_in_app_product(
    package_name: str,
    sku: str,
) -> dict[str, Any]:
    """Delete an in-app product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.delete_in_app_product(package_name=package_name, sku=sku)
    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def batch_get_in_app_products(
    package_name: str,
    skus: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple in-app products at once.

    Args:
        package_name: App package name
        skus: List of product SKUs to retrieve

    Returns:
        List of in-app products, in the same order as requested
    """
    client = get_client_from_context()

    products = client.batch_get_in_app_products(package_name=package_name, skus=skus)
    return [product.model_dump() for product in products]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_delete_in_app_products(
    package_name: str,
    skus: list[str],
) -> dict[str, Any]:
    """Delete multiple in-app products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        skus: List of product SKUs to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_in_app_products"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_in_app_products(package_name=package_name, skus=skus)
    return result.model_dump()


# =============================================================================
# One-Time Product Catalog Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_one_time_product(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Get details of a specific one-time product.

    Args:
        package_name: App package name
        product_id: One-time product ID

    Returns:
        One-time product details including listings and purchase options
    """
    client = get_client_from_context()

    product = client.get_one_time_product(package_name, product_id)
    return product.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_one_time_products(
    package_name: str,
) -> list[dict[str, Any]]:
    """List all one-time products for an app.

    Args:
        package_name: App package name

    Returns:
        List of one-time products
    """
    client = get_client_from_context()

    products = client.list_one_time_products(package_name)
    return [product.model_dump() for product in products]


@mcp.tool(annotations=_READ_TOOL)
def batch_get_one_time_products(
    package_name: str,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple one-time products at once.

    Args:
        package_name: App package name
        product_ids: List of one-time product IDs to retrieve

    Returns:
        List of one-time products
    """
    client = get_client_from_context()

    products = client.batch_get_one_time_products(
        package_name=package_name, product_ids=product_ids
    )
    return [product.model_dump() for product in products]


@mcp.tool(annotations=_WRITE_TOOL)
def patch_one_time_product(
    package_name: str,
    product_id: str,
    product: dict[str, Any],
    update_mask: str,
    regions_version: str = DEFAULT_REGIONS_VERSION,
) -> dict[str, Any]:
    """Create or update a one-time product (patch is create-or-update).

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: One-time product ID
        product: Partial OneTimeProduct resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched one-time product
    """
    if blocked := _read_only_block("patch_one_time_product"):
        return blocked
    client = get_client_from_context()

    result = client.patch_one_time_product(
        package_name=package_name,
        product_id=product_id,
        product=product,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_one_time_product(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Delete a one-time product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: One-time product ID

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_one_time_product"):
        return blocked
    client = get_client_from_context()

    result = client.delete_one_time_product(package_name=package_name, product_id=product_id)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_one_time_products(
    package_name: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple one-time products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of UpdateOneTimeProductRequest bodies (each with oneTimeProduct,
            updateMask, and optional regionsVersion / allowMissing)

    Returns:
        List of updated one-time products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_one_time_products"):
        return blocked
    client = get_client_from_context()

    products = client.batch_update_one_time_products(package_name=package_name, requests=requests)
    return [product.model_dump() for product in products]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_delete_one_time_products(
    package_name: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple one-time products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of DeleteOneTimeProductRequest bodies (each with productId
            and optional packageName / latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_one_time_products"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_one_time_products(package_name=package_name, requests=requests)
    return result.model_dump()


# =============================================================================
# One-Time Product Purchase Option Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def batch_delete_purchase_options(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple purchase options from a one-time product in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        requests: List of DeletePurchaseOptionRequest bodies (each with purchaseOptionId
            and optional latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_purchase_options"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_purchase_options(
        package_name=package_name, product_id=product_id, requests=requests
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_purchase_option_states(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple purchase options in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        requests: List of UpdatePurchaseOptionStateRequest bodies (each with a nested
            activatePurchaseOptionRequest or deactivatePurchaseOptionRequest)

    Returns:
        List of updated one-time products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_states"):
        return blocked
    client = get_client_from_context()

    products = client.batch_update_purchase_option_states(
        package_name=package_name, product_id=product_id, requests=requests
    )
    return [product.model_dump() for product in products]


# =============================================================================
# One-Time Product Purchase Option Offer Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
) -> list[dict[str, Any]]:
    """List all offers for a one-time product purchase option.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard lists across products)
        purchase_option_id: Parent purchase option ID ('-' wildcard lists across options)

    Returns:
        List of one-time product offers
    """
    client = get_client_from_context()

    offers = client.list_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_READ_TOOL)
def batch_get_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Get details for multiple one-time product offers at once.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of GetOneTimeProductOfferRequest bodies

    Returns:
        List of one-time product offers
    """
    client = get_client_from_context()

    offers = client.batch_get_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def activate_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Activate a one-time product offer, making it available to eligible buyers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to activate

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("activate_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.activate_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def deactivate_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Deactivate a one-time product offer, making it unavailable to new buyers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to deactivate

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("deactivate_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def cancel_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Cancel a one-time product offer (e.g. a pre-order offer).

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to cancel

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("cancel_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Create or update multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of UpdateOneTimeProductOfferRequest bodies (each with
            oneTimeProductOffer, updateMask, and optional allowMissing / latencyTolerance)

    Returns:
        List of updated one-time product offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_offers"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_purchase_option_offer_states(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate, deactivate or cancel multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of UpdateOneTimeProductOfferStateRequest bodies (each with a nested
            activate/deactivate/cancel one-time product offer request)

    Returns:
        List of updated one-time product offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_offer_states"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_purchase_option_offer_states(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_delete_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of DeleteOneTimeProductOfferRequest bodies (each with offerId
            and optional latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_purchase_option_offers"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return result.model_dump()


# =============================================================================
# Subscription Catalog Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_subscription(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Get details of a specific subscription product.

    Args:
        package_name: App package name
        product_id: Subscription product ID

    Returns:
        Subscription product details including base plans
    """
    client = get_client_from_context()

    subscription = client.get_subscription(package_name, product_id)
    return subscription.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def create_subscription(
    package_name: str,
    product_id: str,
    subscription: dict[str, Any],
    regions_version: str = DEFAULT_REGIONS_VERSION,
) -> dict[str, Any]:
    """Create a new subscription product in the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID
        subscription: Subscription resource body (e.g. basePlans, listings)
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The created subscription product
    """
    if blocked := _read_only_block("create_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.create_subscription(
        package_name=package_name,
        product_id=product_id,
        subscription=subscription,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def patch_subscription(
    package_name: str,
    product_id: str,
    subscription: dict[str, Any],
    update_mask: str,
    regions_version: str = DEFAULT_REGIONS_VERSION,
) -> dict[str, Any]:
    """Partially update an existing subscription product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID
        subscription: Partial Subscription resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched subscription product
    """
    if blocked := _read_only_block("patch_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.patch_subscription(
        package_name=package_name,
        product_id=product_id,
        subscription=subscription,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_subscription(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Delete a subscription product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.delete_subscription(package_name=package_name, product_id=product_id)
    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def batch_get_subscriptions(
    package_name: str,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple subscription products at once.

    Args:
        package_name: App package name
        product_ids: List of subscription product IDs to retrieve

    Returns:
        List of subscription products
    """
    client = get_client_from_context()

    subscriptions = client.batch_get_subscriptions(
        package_name=package_name, product_ids=product_ids
    )
    return [sub.model_dump() for sub in subscriptions]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_subscriptions(
    package_name: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple subscription products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of UpdateSubscriptionRequest bodies (each with subscription,
            updateMask, and optional regionsVersion)

    Returns:
        List of updated subscription products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_subscriptions"):
        return blocked
    client = get_client_from_context()

    subscriptions = client.batch_update_subscriptions(package_name=package_name, requests=requests)
    return [sub.model_dump() for sub in subscriptions]


# =============================================================================
# Subscription Base Plan Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def activate_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Activate a subscription base plan, making it available to new subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to activate

    Returns:
        The updated subscription product
    """
    if blocked := _read_only_block("activate_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.activate_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def deactivate_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Deactivate a subscription base plan, making it unavailable to new subscribers.

    Existing subscribers keep their subscription. Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to deactivate

    Returns:
        The updated subscription product
    """
    if blocked := _read_only_block("deactivate_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Delete a subscription base plan.

    Only inactive base plans with no active subscribers can be deleted.
    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.delete_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def migrate_base_plan_prices(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Migrate subscribers to the current base plan prices.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID whose prices to migrate
        request: MigrateBasePlanPricesRequest body (e.g. regionalPriceMigrations,
            regionsVersion)

    Returns:
        The MigrateBasePlanPricesResponse (raw dict), or an error object in read-only mode
    """
    if blocked := _read_only_block("migrate_base_plan_prices"):
        return blocked
    client = get_client_from_context()

    return client.migrate_base_plan_prices(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        request=request,
    )


@mcp.tool(annotations=_WRITE_TOOL)
def batch_migrate_base_plan_prices(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Migrate prices for multiple base plans in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        requests: List of MigrateBasePlanPricesRequest bodies

    Returns:
        The BatchMigrateBasePlanPricesResponse (raw dict), or an error object in
        read-only mode
    """
    if blocked := _read_only_block("batch_migrate_base_plan_prices"):
        return blocked
    client = get_client_from_context()

    return client.batch_migrate_base_plan_prices(
        package_name=package_name,
        product_id=product_id,
        requests=requests,
    )


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_base_plan_states(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple base plans in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        requests: List of UpdateBasePlanStateRequest bodies (each with a nested
            activateBasePlanRequest or deactivateBasePlanRequest)

    Returns:
        The updated subscriptions, one per request
    """
    if blocked := _read_only_block("batch_update_base_plan_states"):
        return blocked
    client = get_client_from_context()

    results = client.batch_update_base_plan_states(
        package_name=package_name,
        product_id=product_id,
        requests=requests,
    )
    return [result.model_dump() for result in results]


# =============================================================================
# Subscription Offer Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Get details of a specific subscription offer.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID

    Returns:
        The subscription offer details
    """
    client = get_client_from_context()

    offer = client.get_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return offer.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> list[dict[str, Any]]:
    """List all offers for a subscription base plan.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard lists offers across base plans)

    Returns:
        List of subscription offers
    """
    client = get_client_from_context()

    offers = client.list_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def create_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
    offer: dict[str, Any],
    regions_version: str = DEFAULT_REGIONS_VERSION,
) -> dict[str, Any]:
    """Create a new subscription offer.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID
        offer: SubscriptionOffer resource body (e.g. phases, regionalConfigs, targeting)
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The created subscription offer
    """
    if blocked := _read_only_block("create_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.create_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
        offer=offer,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def patch_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
    offer: dict[str, Any],
    update_mask: str,
    regions_version: str = DEFAULT_REGIONS_VERSION,
) -> dict[str, Any]:
    """Partially update an existing subscription offer.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID
        offer: Partial SubscriptionOffer resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched subscription offer
    """
    if blocked := _read_only_block("patch_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.patch_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
        offer=offer,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def activate_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Activate a subscription offer, making it available to eligible subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to activate

    Returns:
        The updated subscription offer
    """
    if blocked := _read_only_block("activate_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.activate_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def deactivate_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Deactivate a subscription offer, making it unavailable to new subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to deactivate

    Returns:
        The updated subscription offer
    """
    if blocked := _read_only_block("deactivate_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Delete a subscription offer.

    Only inactive offers with no active subscribers can be deleted.
    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.delete_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def batch_get_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Get details for multiple subscription offers in a single operation.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of GetSubscriptionOfferRequest bodies

    Returns:
        List of subscription offers
    """
    client = get_client_from_context()

    offers = client.batch_get_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple subscription offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of UpdateSubscriptionOfferRequest bodies (each with
            subscriptionOffer, updateMask, and optional regionsVersion)

    Returns:
        List of updated subscription offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_subscription_offers"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool(annotations=_WRITE_TOOL)
def batch_update_subscription_offer_states(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple subscription offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of UpdateSubscriptionOfferStateRequest bodies (each with a
            nested activateSubscriptionOfferRequest or deactivateSubscriptionOfferRequest)

    Returns:
        The updated subscription offers, one per request
    """
    if blocked := _read_only_block("batch_update_subscription_offer_states"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_subscription_offer_states(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


# =============================================================================
# Store Listings Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_listing(
    package_name: str,
    language: str = "en-US",
) -> dict[str, Any]:
    """Get store listing for a specific language.

    Args:
        package_name: App package name
        language: Language code (e.g., en-US, es-ES, fr-FR)

    Returns:
        Store listing with title, descriptions, and video
    """
    client = get_client_from_context()

    listing = client.get_listing(package_name, language)
    return listing.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_listing(
    package_name: str,
    language: str,
    title: str | None = None,
    full_description: str | None = None,
    short_description: str | None = None,
    video: str | None = None,
) -> dict[str, Any]:
    """Update store listing for a specific language.

    Args:
        package_name: App package name
        language: Language code (e.g., en-US, es-ES, fr-FR)
        title: App title (max 50 characters, optional)
        full_description: Full description (max 4000 characters, optional)
        short_description: Short description (max 80 characters, optional)
        video: YouTube video URL (optional)

    Returns:
        Update result with success status
    """
    if blocked := _read_only_block("update_listing"):
        return blocked
    client = get_client_from_context()

    result = client.update_listing(
        package_name=package_name,
        language=language,
        title=title,
        full_description=full_description,
        short_description=short_description,
        video=video,
    )
    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_all_listings(package_name: str) -> list[dict[str, Any]]:
    """List all store listings for all languages.

    Args:
        package_name: App package name

    Returns:
        List of store listings for all configured languages
    """
    client = get_client_from_context()

    listings = client.list_all_listings(package_name)
    return [listing.model_dump() for listing in listings]


# =============================================================================
# Testers Management Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_testers(
    package_name: str,
    track: str,
) -> dict[str, Any]:
    """Get testers for a specific testing track.

    Args:
        package_name: App package name
        track: Track name (internal, alpha, beta)

    Returns:
        Tester information with list of email addresses
    """
    client = get_client_from_context()

    testers = client.get_testers(package_name, track)
    return testers.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_testers(
    package_name: str,
    track: str,
    google_groups: list[str],
) -> dict[str, Any]:
    """Update testers for a specific testing track.

    Args:
        package_name: App package name
        track: Track name (internal, alpha, beta)
        google_groups: List of Google Group email addresses

    Returns:
        Update result with success status
    """
    if blocked := _read_only_block("update_testers"):
        return blocked
    client = get_client_from_context()

    result = client.update_testers(package_name, track, google_groups)
    return result


# =============================================================================
# Orders Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_order(
    package_name: str,
    order_id: str,
) -> dict[str, Any]:
    """Get detailed order/transaction information.

    Args:
        package_name: App package name
        order_id: Order ID to retrieve

    Returns:
        Order details including product, purchase state, and token
    """
    client = get_client_from_context()

    order = client.get_order(package_name, order_id)
    return order.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def batch_get_orders(
    package_name: str,
    order_ids: list[str],
) -> list[dict[str, Any]]:
    """Get detailed information for multiple orders at once.

    Args:
        package_name: App package name
        order_ids: List of order IDs to retrieve (1-1000)

    Returns:
        List of order details
    """
    client = get_client_from_context()

    orders = client.batch_get_orders(package_name=package_name, order_ids=order_ids)
    return [order.model_dump() for order in orders]


# =============================================================================
# External Transactions Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_external_transaction(
    package_name: str,
    external_transaction_id: str,
) -> dict[str, Any]:
    """Get an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID

    Returns:
        External transaction details including state, amounts, and create time
    """
    client = get_client_from_context()

    transaction = client.get_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
    )
    return transaction.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def create_external_transaction(
    package_name: str,
    external_transaction_id: str,
    transaction: dict[str, Any],
) -> dict[str, Any]:
    """Create an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID to assign
        transaction: ExternalTransaction resource body

    Returns:
        The created external transaction
    """
    if blocked := _read_only_block("create_external_transaction"):
        return blocked
    client = get_client_from_context()

    result = client.create_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
        transaction=transaction,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def refund_external_transaction(
    package_name: str,
    external_transaction_id: str,
    refund: dict[str, Any],
) -> dict[str, Any]:
    """Refund an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID to refund
        refund: RefundExternalTransactionRequest body (e.g. refundTime plus
            fullRefund or partialRefund)

    Returns:
        The refunded external transaction
    """
    if blocked := _read_only_block("refund_external_transaction"):
        return blocked
    client = get_client_from_context()

    result = client.refund_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
        refund=refund,
    )
    return result.model_dump()


# =============================================================================
# Device Tier Config Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_device_tier_config(
    package_name: str,
    device_tier_config_id: str,
) -> dict[str, Any]:
    """Get a device tier config.

    Args:
        package_name: App package name
        device_tier_config_id: Device tier config ID

    Returns:
        Device tier config details including device groups, tier set, and country sets
    """
    client = get_client_from_context()

    config = client.get_device_tier_config(
        package_name=package_name,
        device_tier_config_id=device_tier_config_id,
    )
    return config.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_device_tier_configs(package_name: str) -> list[dict[str, Any]]:
    """List all device tier configs for an app.

    Args:
        package_name: App package name

    Returns:
        List of device tier configs with device groups, tier set, and country sets
    """
    client = get_client_from_context()

    configs = client.list_device_tier_configs(package_name)
    return [config.model_dump() for config in configs]


@mcp.tool(annotations=_WRITE_TOOL)
def create_device_tier_config(
    package_name: str,
    config: dict[str, Any],
    allow_unknown_devices: bool = False,
) -> dict[str, Any]:
    """Create a new device tier config.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        config: DeviceTierConfig resource body (deviceGroups, deviceTierSet,
            userCountrySets)
        allow_unknown_devices: Accept device IDs unknown to Play's catalog rather
            than rejecting them (default: False)

    Returns:
        The created device tier config
    """
    if blocked := _read_only_block("create_device_tier_config"):
        return blocked
    client = get_client_from_context()

    result = client.create_device_tier_config(
        package_name=package_name,
        config=config,
        allow_unknown_devices=allow_unknown_devices,
    )
    return result.model_dump()


# =============================================================================
# Account Access Tools (Users & Grants)
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_users(developer_id: str) -> list[dict[str, Any]]:
    """List users with access to a developer account.

    Args:
        developer_id: Developer account ID

    Returns:
        List of users with their access state and account-level permissions
    """
    client = get_client_from_context()

    users = client.list_users(developer_id)
    return [user.model_dump() for user in users]


@mcp.tool(annotations=_WRITE_TOOL)
def create_user(developer_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """Grant a user access to a developer account.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        user: User resource body (email, developerAccountPermissions,
            expirationTime, grants)

    Returns:
        The created user
    """
    if blocked := _read_only_block("create_user"):
        return blocked
    client = get_client_from_context()

    result = client.create_user(developer_id=developer_id, user=user)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_user(
    developer_id: str,
    email: str,
    user: dict[str, Any],
    update_mask: str,
) -> dict[str, Any]:
    """Update a user's account access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to update
        user: User resource body with the fields to update
        update_mask: Comma-separated list of fields to update (e.g.
            "developerAccountPermissions,expirationTime")

    Returns:
        The updated user
    """
    if blocked := _read_only_block("update_user"):
        return blocked
    client = get_client_from_context()

    result = client.update_user(
        developer_id=developer_id,
        email=email,
        user=user,
        update_mask=update_mask,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_user(developer_id: str, email: str) -> dict[str, Any]:
    """Remove a user's access to a developer account.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to remove

    Returns:
        Access result with success status
    """
    if blocked := _read_only_block("delete_user"):
        return blocked
    client = get_client_from_context()

    result = client.delete_user(developer_id=developer_id, email=email)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def create_grant(developer_id: str, email: str, grant: dict[str, Any]) -> dict[str, Any]:
    """Grant a user app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to grant access to
        grant: Grant resource body (packageName, appLevelPermissions)

    Returns:
        The created grant
    """
    if blocked := _read_only_block("create_grant"):
        return blocked
    client = get_client_from_context()

    result = client.create_grant(developer_id=developer_id, email=email, grant=grant)
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def update_grant(
    developer_id: str,
    email: str,
    package_name: str,
    grant: dict[str, Any],
    update_mask: str,
) -> dict[str, Any]:
    """Update a user's app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user the grant belongs to
        package_name: App package name the grant applies to
        grant: Grant resource body with the fields to update
        update_mask: Comma-separated list of fields to update (e.g.
            "appLevelPermissions")

    Returns:
        The updated grant
    """
    if blocked := _read_only_block("update_grant"):
        return blocked
    client = get_client_from_context()

    result = client.update_grant(
        developer_id=developer_id,
        email=email,
        package_name=package_name,
        grant=grant,
        update_mask=update_mask,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_grant(developer_id: str, email: str, package_name: str) -> dict[str, Any]:
    """Remove a user's app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user the grant belongs to
        package_name: App package name the grant applies to

    Returns:
        Access result with success status
    """
    if blocked := _read_only_block("delete_grant"):
        return blocked
    client = get_client_from_context()

    result = client.delete_grant(
        developer_id=developer_id,
        email=email,
        package_name=package_name,
    )
    return result.model_dump()


# =============================================================================
# Data Safety Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def set_data_safety(
    package_name: str,
    safety_labels: dict[str, Any],
) -> dict[str, Any]:
    """Write the data safety labels declaration of an app.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        safety_labels: SafetyLabelsUpdateRequest resource body. Contains a
            `safetyLabels` string with the contents of the Data Safety CSV

    Returns:
        The result of the update
    """
    if blocked := _read_only_block("set_data_safety"):
        return blocked
    client = get_client_from_context()

    result = client.set_data_safety(
        package_name=package_name,
        safety_labels=safety_labels,
    )
    return result.model_dump()


# =============================================================================
# App Recovery Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_app_recoveries(package_name: str, version_code: int) -> list[dict[str, Any]]:
    """List all app recovery actions for an app version.

    Args:
        package_name: App package name
        version_code: App version code the recovery actions target

    Returns:
        List of app recovery actions with ID, status, targeting, and create time
    """
    client = get_client_from_context()

    recoveries = client.list_app_recoveries(package_name, version_code)
    return [recovery.model_dump() for recovery in recoveries]


@mcp.tool(annotations=_WRITE_TOOL)
def create_app_recovery(
    package_name: str,
    recovery: dict[str, Any],
) -> dict[str, Any]:
    """Create a draft app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        recovery: CreateDraftAppRecoveryRequest resource body (e.g.
            `remoteInAppUpdate` plus `targeting`)

    Returns:
        The created app recovery action
    """
    if blocked := _read_only_block("create_app_recovery"):
        return blocked
    client = get_client_from_context()

    result = client.create_app_recovery(
        package_name=package_name,
        recovery=recovery,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def deploy_app_recovery(
    package_name: str,
    app_recovery_id: str,
) -> dict[str, Any]:
    """Deploy an app recovery action to users.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID

    Returns:
        The result of the deploy action
    """
    if blocked := _read_only_block("deploy_app_recovery"):
        return blocked
    client = get_client_from_context()

    result = client.deploy_app_recovery(
        package_name=package_name,
        app_recovery_id=app_recovery_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def cancel_app_recovery(
    package_name: str,
    app_recovery_id: str,
) -> dict[str, Any]:
    """Cancel an app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID

    Returns:
        The result of the cancel action
    """
    if blocked := _read_only_block("cancel_app_recovery"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_app_recovery(
        package_name=package_name,
        app_recovery_id=app_recovery_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def add_app_recovery_targeting(
    package_name: str,
    app_recovery_id: str,
    targeting: dict[str, Any],
) -> dict[str, Any]:
    """Add targeting to an app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID
        targeting: AddTargetingRequest resource body (e.g. a `targetingUpdate`
            object)

    Returns:
        The result of the add-targeting action
    """
    if blocked := _read_only_block("add_app_recovery_targeting"):
        return blocked
    client = get_client_from_context()

    result = client.add_app_recovery_targeting(
        package_name=package_name,
        app_recovery_id=app_recovery_id,
        targeting=targeting,
    )
    return result.model_dump()


# =============================================================================
# Generated APKs Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_generated_apks(
    package_name: str,
    version_code: int,
) -> list[dict[str, Any]]:
    """List the APKs Google Play generated from an app bundle version.

    Returns one entry per downloadable generated APK (split, standalone,
    universal, asset pack slice, or recovery), each with a download ID that can
    be passed to `download_generated_apk`.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle

    Returns:
        List of downloadable generated APKs with their download IDs and types
    """
    client = get_client_from_context()

    downloads = client.list_generated_apks(
        package_name=package_name,
        version_code=version_code,
    )
    return [download.model_dump() for download in downloads]


@mcp.tool(annotations=_READ_TOOL)
def download_generated_apk(
    package_name: str,
    version_code: int,
    download_id: str,
    destination_path: str,
) -> dict[str, Any]:
    """Download a single generated APK to a local file.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        download_id: Download ID of the generated APK (from `list_generated_apks`)
        destination_path: Local path to write the APK bytes to

    Returns:
        Download result with success status and destination path
    """
    client = get_client_from_context()

    result = client.download_generated_apk(
        package_name=package_name,
        version_code=version_code,
        download_id=download_id,
        destination_path=destination_path,
    )
    return result.model_dump()


# =============================================================================
# System APK Variants Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_system_apk_variant(
    package_name: str,
    version_code: int,
    variant_id: int,
) -> dict[str, Any]:
    """Get a previously created system APK variant.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant_id: ID of the system APK variant

    Returns:
        System APK variant details including device spec and options
    """
    client = get_client_from_context()

    variant = client.get_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant_id=variant_id,
    )
    return variant.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def list_system_apk_variants(
    package_name: str,
    version_code: int,
) -> list[dict[str, Any]]:
    """List previously created system APK variants for an app bundle version.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle

    Returns:
        List of system APK variants with their IDs, device specs, and options
    """
    client = get_client_from_context()

    variants = client.list_system_apk_variants(
        package_name=package_name,
        version_code=version_code,
    )
    return [variant.model_dump() for variant in variants]


@mcp.tool(annotations=_WRITE_TOOL)
def create_system_apk_variant(
    package_name: str,
    version_code: int,
    variant: dict[str, Any],
) -> dict[str, Any]:
    """Create a system APK variant from an uploaded app bundle.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant: Variant resource body (e.g. `deviceSpec` and `options`)

    Returns:
        The created system APK variant
    """
    if blocked := _read_only_block("create_system_apk_variant"):
        return blocked
    client = get_client_from_context()

    result = client.create_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant=variant,
    )
    return result.model_dump()


@mcp.tool(annotations=_READ_TOOL)
def download_system_apk_variant(
    package_name: str,
    version_code: int,
    variant_id: int,
    destination_path: str,
) -> dict[str, Any]:
    """Download a previously created system APK variant to a local file.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant_id: ID of the system APK variant (from `list_system_apk_variants`)
        destination_path: Local path to write the APK bytes to

    Returns:
        Download result with success status and destination path
    """
    client = get_client_from_context()

    result = client.download_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant_id=variant_id,
        destination_path=destination_path,
    )
    return result.model_dump()


# =============================================================================
# Expansion Files Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def get_expansion_file(
    package_name: str,
    version_code: int,
    expansion_file_type: str = "main",
) -> dict[str, Any]:
    """Get APK expansion file information.

    Expansion files are used for large apps (especially games) that exceed
    the 100MB APK size limit.

    Args:
        package_name: App package name
        version_code: APK version code
        expansion_file_type: Type of expansion file (main or patch)

    Returns:
        Expansion file information including size and references
    """
    client = get_client_from_context()

    expansion_file = client.get_expansion_file(package_name, version_code, expansion_file_type)
    return expansion_file.model_dump()


# =============================================================================
# Edit Upload Tools (APKs, bundles, deobfuscation & expansion files)
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_apks(package_name: str) -> list[dict[str, Any]]:
    """List the APKs currently uploaded for an app.

    Args:
        package_name: App package name

    Returns:
        List of APKs, each with its version code and binary sha1/sha256 hashes
    """
    client = get_client_from_context()

    apks = client.list_apks(package_name)
    return [apk.model_dump() for apk in apks]


@mcp.tool(annotations=_READ_TOOL)
def list_bundles(package_name: str) -> list[dict[str, Any]]:
    """List the Android App Bundles currently uploaded for an app.

    Args:
        package_name: App package name

    Returns:
        List of app bundles, each with its version code and sha1/sha256 hashes
    """
    client = get_client_from_context()

    bundles = client.list_bundles(package_name)
    return [bundle.model_dump() for bundle in bundles]


@mcp.tool(annotations=_WRITE_TOOL)
def upload_apk(package_name: str, apk_path: str, commit: bool = True) -> dict[str, Any]:
    """Upload an APK to a new edit, committing it unless commit=False.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        apk_path: Local path to the APK file
        commit: Commit the edit on success (default). Pass False to have Play
            validate the APK and then discard the edit, leaving no draft on the
            Console and no version code consumed

    Returns:
        The uploaded APK with its version code, binary sha1/sha256 hashes, and
        whether the edit was committed
    """
    if blocked := _read_only_block("upload_apk"):
        return blocked
    client = get_client_from_context()

    apk = client.upload_apk(package_name=package_name, apk_path=apk_path, commit=commit)
    return _upload_result(apk.model_dump(), commit=commit)


@mcp.tool(annotations=_WRITE_TOOL)
def upload_bundle(package_name: str, bundle_path: str, commit: bool = True) -> dict[str, Any]:
    """Upload an Android App Bundle (.aab) to a new edit, committing it unless commit=False.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        bundle_path: Local path to the app bundle (.aab) file
        commit: Commit the edit on success (default). Pass False to have Play
            validate the bundle and then discard the edit, leaving no draft on
            the Console and no version code consumed — use this to tell a
            rejected artifact apart from a failing Play backend

    Returns:
        The uploaded app bundle with its version code, sha1/sha256 hashes, and
        whether the edit was committed
    """
    if blocked := _read_only_block("upload_bundle"):
        return blocked
    client = get_client_from_context()

    bundle = client.upload_bundle(package_name=package_name, bundle_path=bundle_path, commit=commit)
    return _upload_result(bundle.model_dump(), commit=commit)


@mcp.tool(annotations=_WRITE_TOOL)
def upload_deobfuscation_file(
    package_name: str,
    version_code: int,
    file_path: str,
    deobfuscation_file_type: str = "proguard",
) -> dict[str, Any]:
    """Upload a deobfuscation (ProGuard mapping or native symbols) file.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: APK version code the file applies to
        file_path: Local path to the deobfuscation file
        deobfuscation_file_type: Type of file - one of: proguard, nativeCode

    Returns:
        The uploaded deobfuscation file configuration with its symbol type
    """
    if blocked := _read_only_block("upload_deobfuscation_file"):
        return blocked
    client = get_client_from_context()

    deobfuscation_file = client.upload_deobfuscation_file(
        package_name=package_name,
        version_code=version_code,
        file_path=file_path,
        deobfuscation_file_type=deobfuscation_file_type,
    )
    return deobfuscation_file.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def upload_expansion_file(
    package_name: str,
    version_code: int,
    file_path: str,
    expansion_file_type: str = "main",
) -> dict[str, Any]:
    """Upload an APK expansion file (OBB) to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: APK version code the file applies to
        file_path: Local path to the expansion file
        expansion_file_type: Type of expansion file - one of: main, patch

    Returns:
        The uploaded expansion file information including size and references
    """
    if blocked := _read_only_block("upload_expansion_file"):
        return blocked
    client = get_client_from_context()

    expansion_file = client.upload_expansion_file(
        package_name=package_name,
        version_code=version_code,
        file_path=file_path,
        expansion_file_type=expansion_file_type,
    )
    return expansion_file.model_dump()


# =============================================================================
# Store Listing Image Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def list_images(package_name: str, language: str, image_type: str) -> list[dict[str, Any]]:
    """List the store-listing images for a language and image type.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner

    Returns:
        List of images, each with its ID, serving URL and sha1/sha256 hashes
    """
    client = get_client_from_context()

    images = client.list_images(package_name, language, image_type)
    return [image.model_dump() for image in images]


@mcp.tool(annotations=_WRITE_TOOL)
def upload_image(
    package_name: str, language: str, image_type: str, image_path: str
) -> dict[str, Any]:
    """Upload a store-listing image (PNG or JPEG) to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner
        image_path: Local path to the image file (PNG or JPEG)

    Returns:
        The uploaded image with its ID, serving URL and sha1/sha256 hashes
    """
    if blocked := _read_only_block("upload_image"):
        return blocked
    client = get_client_from_context()

    image = client.upload_image(
        package_name=package_name,
        language=language,
        image_type=image_type,
        image_path=image_path,
    )
    return image.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_image(
    package_name: str, language: str, image_type: str, image_id: str
) -> dict[str, Any]:
    """Delete a single store-listing image by ID and commit the edit.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner
        image_id: Unique identifier of the image to delete

    Returns:
        Delete result with success status and deleted count
    """
    if blocked := _read_only_block("delete_image"):
        return blocked
    client = get_client_from_context()

    result = client.delete_image(
        package_name=package_name,
        language=language,
        image_type=image_type,
        image_id=image_id,
    )
    return result.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def delete_all_images(package_name: str, language: str, image_type: str) -> dict[str, Any]:
    """Delete all store-listing images for a language and image type; commit the edit.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type to clear all images for - one of: phoneScreenshots,
            sevenInchScreenshots, tenInchScreenshots, tvScreenshots, wearScreenshots,
            icon, featureGraphic, tvBanner

    Returns:
        Delete result with success status and the number of images deleted
    """
    if blocked := _read_only_block("delete_all_images"):
        return blocked
    client = get_client_from_context()

    result = client.delete_all_images(
        package_name=package_name,
        language=language,
        image_type=image_type,
    )
    return result.model_dump()


# =============================================================================
# Validation Tools
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def validate_package_name(package_name: str) -> dict[str, Any]:
    """Validate package name format before using it in other operations.

    Args:
        package_name: Package name to validate (e.g., com.example.myapp)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_package_name(package_name)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
        "package_name": package_name,
    }


@mcp.tool(annotations=_READ_TOOL)
def validate_track(track: str) -> dict[str, Any]:
    """Validate track name before using it in deployment operations.

    Args:
        track: Track name to validate (internal, alpha, beta, production)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_track(track)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
        "track": track,
    }


@mcp.tool(annotations=_READ_TOOL)
def validate_listing_text(
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
) -> dict[str, Any]:
    """Validate store listing text lengths before updating.

    Args:
        title: App title (max 50 characters)
        short_description: Short description (max 80 characters)
        full_description: Full description (max 4000 characters)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_listing_text(title, short_description, full_description)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
    }


# =============================================================================
# Batch Operations Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def batch_deploy(
    package_name: str,
    file_path: str,
    tracks: list[str],
    release_notes: str | None = None,
    rollout_percentages: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Deploy an app to multiple tracks in a single operation.

    This is useful for deploying to internal and alpha tracks simultaneously,
    or for promoting to multiple testing tracks at once.

    Args:
        package_name: App package name
        file_path: Absolute path to APK or AAB file
        tracks: List of tracks to deploy to (e.g., ["internal", "alpha"])
        release_notes: Optional release notes for all tracks
        rollout_percentages: Optional dict mapping track names to rollout percentages

    Returns:
        Batch deployment result with individual results for each track
    """
    if blocked := _read_only_block("batch_deploy"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}

    if rollout_percentages:
        for track_name, pct in rollout_percentages.items():
            if not (0.0 < pct <= 100.0):
                return {
                    "error": f"rollout_percentage for track '{track_name}' must be greater than 0.0 and at most 100.0"
                }

    client = get_client_from_context()

    result = client.batch_deploy(
        package_name=package_name,
        file_path=file_path,
        tracks=tracks,
        release_notes=release_notes,
        rollout_percentages=rollout_percentages,
    )
    return result.model_dump()


# =============================================================================
# Internal App Sharing Tools
# =============================================================================


@mcp.tool(annotations=_WRITE_TOOL)
def upload_internal_app_sharing_apk(
    package_name: str,
    apk_path: str,
) -> dict[str, Any]:
    """Upload an APK to internal app sharing.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        apk_path: Local path to the APK file

    Returns:
        The uploaded artifact with download URL, certificate fingerprint, and sha256
    """
    if blocked := _read_only_block("upload_internal_app_sharing_apk"):
        return blocked
    client = get_client_from_context()

    artifact = client.upload_internal_app_sharing_apk(
        package_name=package_name,
        apk_path=apk_path,
    )
    return artifact.model_dump()


@mcp.tool(annotations=_WRITE_TOOL)
def upload_internal_app_sharing_bundle(
    package_name: str,
    bundle_path: str,
) -> dict[str, Any]:
    """Upload an app bundle (.aab) to internal app sharing.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        bundle_path: Local path to the app bundle (.aab) file

    Returns:
        The uploaded artifact with download URL, certificate fingerprint, and sha256
    """
    if blocked := _read_only_block("upload_internal_app_sharing_bundle"):
        return blocked
    client = get_client_from_context()

    artifact = client.upload_internal_app_sharing_bundle(
        package_name=package_name,
        bundle_path=bundle_path,
    )
    return artifact.model_dump()


# =============================================================================
# BigQuery tools (raw Firebase export: Analytics events, Crashlytics, Sessions,
# Performance Monitoring — whatever datasets are linked in the GCP project)
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def bigquery_list_datasets(project_id: str) -> dict[str, Any]:
    """List BigQuery datasets visible to the configured service account in a GCP project.

    Args:
        project_id: GCP project id (e.g. the Firebase project's project id)
    """
    client = get_bigquery_client_from_context()
    raw = client.list_datasets(project_id)
    return {
        "projectId": project_id,
        "datasets": [d["datasetReference"]["datasetId"] for d in raw.get("datasets", [])],
    }


@mcp.tool(annotations=_READ_TOOL)
def bigquery_list_tables(project_id: str, dataset_id: str, max_results: int = 50) -> dict[str, Any]:
    """List tables in a BigQuery dataset (e.g. Firebase Analytics' daily events_YYYYMMDD tables).

    Args:
        project_id: GCP project id
        dataset_id: Dataset id (e.g. "analytics_<property_id>", "firebase_crashlytics")
        max_results: Max tables to return (default 50)
    """
    client = get_bigquery_client_from_context()
    raw = client.list_tables(project_id, dataset_id, max_results)
    return {
        "projectId": project_id,
        "datasetId": dataset_id,
        "totalItems": raw.get("totalItems", 0),
        "tables": [t["tableReference"]["tableId"] for t in raw.get("tables", [])],
    }


@mcp.tool(annotations=_READ_TOOL)
def bigquery_get_table_schema(project_id: str, dataset_id: str, table_id: str) -> dict[str, Any]:
    """Get a BigQuery table's schema (field names/types) plus row/byte counts.

    Args:
        project_id: GCP project id
        dataset_id: Dataset id
        table_id: Table id
    """
    client = get_bigquery_client_from_context()
    return client.get_table_schema(project_id, dataset_id, table_id)


@mcp.tool(annotations=_READ_TOOL)
def bigquery_execute_query(
    project_id: str,
    query: str,
    max_results: int = 100,
    max_bytes_billed: int = 1_000_000_000,
) -> dict[str, Any]:
    """Run a read-only standard-SQL query against BigQuery.

    The service account only has the bigquery.readonly scope (no
    INSERT/UPDATE/DELETE/DDL). max_bytes_billed is an extra cost guardrail —
    a query that would scan more bytes than this fails instead of running
    (default 1 GB).

    Args:
        project_id: GCP project id
        query: Standard SQL query, e.g.
            "SELECT event_name, COUNT(*) n FROM `proj.analytics_123.events_*`
             WHERE _TABLE_SUFFIX BETWEEN '20260701' AND '20260707'
             GROUP BY event_name ORDER BY n DESC"
        max_results: Max rows to return (default 100)
        max_bytes_billed: Cost guardrail in bytes (default 1 GB)
    """
    cap = _bigquery_max_bytes_cap()
    if max_bytes_billed <= 0 or max_bytes_billed > cap:
        return {
            "error": (
                f"max_bytes_billed must be between 1 and {cap} bytes "
                "(operator cap: PLAY_STORE_MCP_BIGQUERY_MAX_BYTES_BILLED)"
            )
        }
    client = get_bigquery_client_from_context()
    raw = client.execute_query(project_id, query, max_results, max_bytes_billed)
    fields = [f["name"] for f in raw.get("schema", {}).get("fields", [])]
    rows = [
        {fields[i]: cell.get("v") for i, cell in enumerate(row.get("f", []))}
        for row in raw.get("rows", [])
    ]
    return {
        "totalRows": raw.get("totalRows"),
        "totalBytesProcessed": raw.get("totalBytesProcessed"),
        "totalBytesBilled": raw.get("totalBytesBilled"),
        "jobComplete": raw.get("jobComplete"),
        "rows": rows,
    }


# =============================================================================
# Google Analytics Data API tools (GA4 aggregated reports — server-side
# rollups, distinct from BigQuery's raw per-event rows)
# =============================================================================


@mcp.tool(annotations=_READ_TOOL)
def analytics_run_report(
    property_id: str,
    dimensions: list[str],
    metrics: list[str],
    start_date: str = "7daysAgo",
    end_date: str = "today",
    limit: int = 100,
) -> dict[str, Any]:
    """Run a GA4 aggregated report (e.g. event counts by eventName over a date range).

    Args:
        property_id: GA4 property id (numeric, e.g. "521849462" — not the
            Firebase project id)
        dimensions: GA4 dimension names, e.g. ["eventName"]
        metrics: GA4 metric names, e.g. ["eventCount"]
        start_date: Start of date range, relative ("7daysAgo") or "YYYY-MM-DD"
        end_date: End of date range, relative ("today") or "YYYY-MM-DD"
        limit: Max rows to return (default 100)
    """
    client = get_analytics_client_from_context()
    raw = client.run_report(property_id, dimensions, metrics, start_date, end_date, limit)
    dim_names = [h["name"] for h in raw.get("dimensionHeaders", [])]
    met_names = [h["name"] for h in raw.get("metricHeaders", [])]
    rows = []
    for row in raw.get("rows", []):
        entry = {dim_names[i]: v["value"] for i, v in enumerate(row.get("dimensionValues", []))}
        entry.update({met_names[i]: v["value"] for i, v in enumerate(row.get("metricValues", []))})
        rows.append(entry)
    return {"propertyId": property_id, "rowCount": raw.get("rowCount"), "rows": rows}


@mcp.tool(annotations=_READ_TOOL)
def analytics_run_realtime_report(
    property_id: str,
    dimensions: list[str],
    metrics: list[str],
    limit: int = 100,
) -> dict[str, Any]:
    """Run a GA4 realtime report (active users/events in roughly the last 30 minutes).

    Args:
        property_id: GA4 property id (numeric, e.g. "521849462")
        dimensions: GA4 dimension names, e.g. ["eventName"]
        metrics: GA4 metric names, e.g. ["activeUsers"]
        limit: Max rows to return (default 100)
    """
    client = get_analytics_client_from_context()
    raw = client.run_realtime_report(property_id, dimensions, metrics, limit)
    dim_names = [h["name"] for h in raw.get("dimensionHeaders", [])]
    met_names = [h["name"] for h in raw.get("metricHeaders", [])]
    rows = []
    for row in raw.get("rows", []):
        entry = {dim_names[i]: v["value"] for i, v in enumerate(row.get("dimensionValues", []))}
        entry.update({met_names[i]: v["value"] for i, v in enumerate(row.get("metricValues", []))})
        rows.append(entry)
    return {"propertyId": property_id, "rowCount": raw.get("rowCount"), "rows": rows}


# =============================================================================
# HTTP Endpoints for Streamable Transport
# =============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:  # noqa: ARG001
    """Health check endpoint for monitoring and load balancers."""
    return JSONResponse({"status": "healthy", "service": "play-store-mcp"})


def _env_token(name: str) -> str | None:
    """Read a secret token from the environment, ignoring surrounding whitespace.

    Tokens often come from ``$(cat file)`` or k8s secrets with a trailing newline;
    an unstripped token could never match and the server would give no hint why.
    """
    value = (os.environ.get(name) or "").strip()
    return value or None


def _bearer_matches(header_value: bytes, token: str) -> bool:
    """Constant-time check of an ``Authorization`` header against ``token``.

    The scheme is case-insensitive (RFC 7235) and surrounding whitespace is
    ignored. Works on bytes so non-ASCII header bytes give a clean mismatch.
    """
    scheme, _, value = header_value.strip().partition(b" ")
    if scheme.lower() != b"bearer":
        return False
    return secrets.compare_digest(value.strip(), token.encode("utf-8"))


def _authorize_credentials_request(request: Request) -> JSONResponse | None:
    """Authorize a POST to the /credentials management endpoint.

    If PLAY_STORE_MCP_ADMIN_TOKEN is set, an ``Authorization: Bearer <token>``
    header matching it (constant-time comparison) is required, and the request
    is accepted from any host. This is the correct mode behind a reverse proxy,
    where ``request.client.host`` is the proxy address and cannot be trusted as
    a "localhost" signal.

    If no admin token is configured but PLAY_STORE_MCP_AUTH_TOKEN is, that token
    is required instead: a loopback peer is NOT proof of a local caller when the
    server sits behind a same-host tunnel or proxy (cloudflared, ngrok, ssh -R).

    Only when neither token is configured does the endpoint fall back to accepting
    loopback (localhost) peers only — the historical behavior.

    Returns an error ``JSONResponse`` if the request is not authorized, else None.
    """
    required_token = _env_token("PLAY_STORE_MCP_ADMIN_TOKEN") or _env_token(
        "PLAY_STORE_MCP_AUTH_TOKEN"
    )
    if required_token:
        # Compare as bytes: Starlette decodes header values as latin-1, so encode
        # back to the raw bytes; a crafted header gives a clean 401, never a 500.
        provided = request.headers.get("authorization", "").encode("latin-1", errors="replace")
        if not _bearer_matches(provided, required_token):
            return JSONResponse(
                {"success": False, "error": "Missing or invalid admin token"},
                status_code=401,
            )
        return None

    # No admin token configured: only allow loopback peers.
    client_host = request.client.host if request.client else None
    try:
        is_loopback = client_host is not None and ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        return JSONResponse(
            {
                "success": False,
                "error": (
                    "This endpoint is only accessible from localhost. Set "
                    "PLAY_STORE_MCP_ADMIN_TOKEN and send an 'Authorization: Bearer' header "
                    "to allow authenticated access (required when running behind a proxy, "
                    "where the peer address is the proxy and not the real client)."
                ),
            },
            status_code=403,
        )
    return None


def _parse_base64_credentials_payload(
    credentials_base64: str,
) -> tuple[PlayStoreClient | None, JSONResponse | None]:
    """Decode a base64-encoded service account JSON payload into a new client.

    Returns (client, None) on success, or (None, error_response) on failure.
    """
    try:
        decoded = base64.b64decode(credentials_base64).decode("utf-8")
        credentials_dict = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError) as e:
        return None, JSONResponse(
            {"success": False, "error": f"Invalid base64 encoding: {e}"},
            status_code=400,
        )
    except json.JSONDecodeError:
        return None, JSONResponse(
            {"success": False, "error": "Invalid JSON in base64-decoded credentials"},
            status_code=400,
        )
    return PlayStoreClient(credentials_json=credentials_dict), None


def _parse_inline_credentials_payload(
    credentials: Any,
) -> tuple[PlayStoreClient | None, JSONResponse | None]:
    """Parse an inline ``credentials`` value (JSON string or object) into a new client.

    Returns (client, None) on success, or (None, error_response) on failure.
    """
    if isinstance(credentials, str):
        # Validate it's valid JSON
        try:
            json.loads(credentials)
        except json.JSONDecodeError:
            return None, JSONResponse(
                {"success": False, "error": "Invalid JSON in credentials string"},
                status_code=400,
            )
        return PlayStoreClient(credentials_json=credentials), None
    if isinstance(credentials, dict):
        return PlayStoreClient(credentials_json=credentials), None
    return None, JSONResponse(
        {"success": False, "error": "credentials must be a string or object"},
        status_code=400,
    )


def _verify_credentials_live(client: PlayStoreClient) -> None:
    """Prove the key works by minting an access token before it replaces the shared clients.

    ``_get_service()`` only runs ``build()`` against the static discovery document —
    no network call — so a well-formed but revoked / disabled / wrong-project key
    would otherwise be accepted and swapped in over working credentials.
    Raises PlayStoreClientError when Google rejects the key.
    """
    credentials = client._credentials
    if credentials is None:  # _get_service was stubbed (unit tests); nothing to refresh
        return
    import google.auth.exceptions  # noqa: PLC0415 - local: only needed on this admin path
    import google.auth.transport.requests  # noqa: PLC0415

    try:
        credentials.refresh(google.auth.transport.requests.Request())
    except google.auth.exceptions.GoogleAuthError as e:
        raise PlayStoreClientError(f"Google rejected the credentials: {type(e).__name__}") from e


def _parse_credentials_request_body(
    body: Any,
) -> tuple[PlayStoreClient | None, JSONResponse | None]:
    """Parse a /credentials POST body into a new ``PlayStoreClient``.

    Returns (client, None) on success, or (None, error_response) on failure.
    """
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"success": False, "error": "Request body must be a JSON object"},
            status_code=400,
        )
    credentials = body.get("credentials")
    credentials_base64 = body.get("credentials_base64")

    if not credentials and not credentials_base64:
        return None, JSONResponse(
            {
                "success": False,
                "error": "Missing 'credentials' or 'credentials_base64' in request body",
            },
            status_code=400,
        )

    if credentials_base64:
        return _parse_base64_credentials_payload(credentials_base64)
    return _parse_inline_credentials_payload(credentials)


@mcp.custom_route("/credentials", methods=["POST"])
async def update_credentials(request: Request) -> JSONResponse:
    """Update Google Play Store credentials via HTTP POST.

    Management endpoint - restricted to localhost only.

    This endpoint allows local clients to provide credentials when using
    streamable-http transport. Accepts JSON credentials in the request body.

    Request body should be one of:
    - {"credentials": {...}} - Service account JSON object
    - {"credentials": "..."} - Service account JSON string
    - {"credentials_base64": "..."} - Base64-encoded service account JSON

    Returns:
        JSON response with success status
    """
    # Management endpoint: authorize by admin token (if configured) or localhost.
    auth_error = _authorize_credentials_request(request)
    if auth_error is not None:
        return auth_error

    try:
        body = await request.json()

        new_client, parse_error = _parse_credentials_request_body(body)
        if parse_error is not None:
            return parse_error

        if (
            new_client is None
        ):  # pragma: no cover - defensive; branches above always assign or return
            return JSONResponse(
                {"success": False, "error": "No credentials could be parsed from the request"},
                status_code=400,
            )

        # Validate credentials by attempting to build the service. This does
        # blocking network I/O, so run it off the event loop.
        try:
            await asyncio.to_thread(new_client._get_service)
            await asyncio.to_thread(_verify_credentials_live, new_client)
        except PlayStoreClientError:
            logger.warning("Credential validation failed for /credentials request")
            return JSONResponse(
                {"success": False, "error": "Invalid credentials"},
                status_code=401,
            )

        # Replace every cached client, not just the Play one. Rotation is often
        # a response to a leaked key, so any client left holding the previous
        # credentials would keep using the compromised key until restart.
        rotated = new_client._credentials_json
        _shared_state["client"] = new_client
        _shared_state["crashlytics_client"] = CrashlyticsClient(credentials_json=rotated)
        _shared_state["reporting_client"] = ReportingClient(credentials_json=rotated)
        _shared_state["bigquery_client"] = BigQueryClient(credentials_json=rotated)
        _shared_state["analytics_client"] = AnalyticsDataClient(credentials_json=rotated)
        _shared_state["credentials_updated"] = True

        logger.info("Credentials updated successfully via HTTP endpoint")

        return JSONResponse(
            {"success": True, "message": "Credentials updated successfully"},
            status_code=200,
        )

    except json.JSONDecodeError:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON in request body"},
            status_code=400,
        )
    except Exception:
        logger.exception("Error updating credentials")
        return JSONResponse(
            {"success": False, "error": "Internal server error"},
            status_code=500,
        )


# =============================================================================
# Entry Point
# =============================================================================


def _dns_rebinding_disabled() -> bool:
    """Return True when DNS-rebinding (Host-header) protection is disabled."""
    return bool(os.environ.get("PLAY_STORE_MCP_DISABLE_DNS_REBINDING"))


def _is_wildcard_bind(host: str) -> bool:
    """Return True if host binds all interfaces (unspecified address) or is unset."""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _is_loopback_bind(host: str) -> bool:
    """Return True if host only binds a loopback interface (reachable from this machine only)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Unset, wildcard-like or arbitrary hostnames may resolve to a routable
        # interface; treat them as network-exposed.
        return False


def _allow_unauthenticated() -> bool:
    """Return True when the operator explicitly opted out of HTTP bearer-token auth."""
    return os.environ.get("PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Paths served without the MCP bearer token: /health for load balancers and the
# Docker HEALTHCHECK, and /credentials, which enforces its own authorization
# (PLAY_STORE_MCP_ADMIN_TOKEN in the same Authorization header, else loopback-only).
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/credentials"})


class _BearerTokenAuthMiddleware:
    """Pure-ASGI middleware requiring ``Authorization: Bearer <token>`` on HTTP requests.

    Pure ASGI (not BaseHTTPMiddleware) so streaming SSE responses are not buffered.
    The comparison is constant-time and done on bytes, mirroring
    _authorize_credentials_request.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket") or (
            kind == "http" and scope.get("path", "") in _AUTH_EXEMPT_PATHS
        ):
            # lifespan (and any future non-request scope) passes through.
            await self.app(scope, receive, send)
            return
        provided = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                provided = value
                break
        if provided and _bearer_matches(provided, self._token):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            # Fail closed: reject the handshake (policy violation) without a token.
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"success": False, "error": "Missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def _run_http(transport: str, host: str, port: int) -> None:
    """Serve a network transport with bearer-token auth and DNS-rebinding protection.

    If PLAY_STORE_MCP_AUTH_TOKEN is set, every HTTP request (except /health and
    /credentials, see _AUTH_EXEMPT_PATHS) must carry ``Authorization: Bearer
    <token>``. Binding a non-loopback address without a token refuses to start
    unless PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED is set, since every tool
    (refund_order, deploy_app, ...) runs with the server's service account.

    fastmcp has no constructor-level transport security; we attach Starlette
    TrustedHostMiddleware (which is exactly Host-header validation) unless
    PLAY_STORE_MCP_DISABLE_DNS_REBINDING is set.

    Downloads are always confined to a base directory by the client (defaulting
    to the working directory when PLAY_STORE_MCP_DOWNLOAD_DIR is unset), so a
    network transport is safe to start either way. When the variable is unset we
    only warn — a network-exposed deployment should point it at a writable
    directory to control where APK/AAB downloads land, rather than defaulting to
    the process working directory (which may be read-only on some hosts).
    """
    if not os.environ.get("PLAY_STORE_MCP_DOWNLOAD_DIR"):
        logger.warning(
            "PLAY_STORE_MCP_DOWNLOAD_DIR is not set; APK/AAB downloads will be confined to "
            "the server's working directory. Set it to a writable directory to control where "
            "downloads are written on a network-exposed deployment.",
            transport=transport,
        )
    admin_token = _env_token("PLAY_STORE_MCP_ADMIN_TOKEN")
    if admin_token and len(admin_token) < _MIN_ADMIN_TOKEN_LENGTH:
        logger.warning(
            "PLAY_STORE_MCP_ADMIN_TOKEN is shorter than recommended; a weak token makes "
            "the /credentials endpoint's Bearer-token check easier to brute-force. Use a "
            "long random value, e.g. `openssl rand -hex 32`.",
            token_length=len(admin_token),
            recommended_minimum=_MIN_ADMIN_TOKEN_LENGTH,
        )
    # Marks the process as serving remote callers: uploads then require
    # PLAY_STORE_MCP_UPLOAD_DIR (see PlayStoreClient._confine_upload_path).
    os.environ["PLAY_STORE_MCP_HTTP_MODE"] = "1"
    auth_token = _env_token("PLAY_STORE_MCP_AUTH_TOKEN")
    if not auth_token and not _is_loopback_bind(host):
        if not _allow_unauthenticated():
            message = (
                f"Refusing to serve {transport} on non-loopback host {host!r} without "
                "authentication: anyone who can reach the port could call every tool with "
                "the server's service account. Set PLAY_STORE_MCP_AUTH_TOKEN (e.g. "
                "`openssl rand -hex 32`) and send 'Authorization: Bearer <token>', or set "
                "PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED=1 if an authenticating proxy fronts "
                "this server, or if it holds no server-side credentials and callers supply "
                "their own via X-Google-Credentials."
            )
            logger.error(message, host=host, transport=transport)
            raise SystemExit(message)
        logger.warning(
            "Serving without authentication on a non-loopback host "
            "(PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED is set)",
            host=host,
        )
    if auth_token and len(auth_token) < _MIN_ADMIN_TOKEN_LENGTH:
        logger.warning(
            "PLAY_STORE_MCP_AUTH_TOKEN is shorter than recommended; use a long random "
            "value, e.g. `openssl rand -hex 32`.",
            token_length=len(auth_token),
            recommended_minimum=_MIN_ADMIN_TOKEN_LENGTH,
        )
    middleware: list[Middleware] = []
    if not _dns_rebinding_disabled():
        allowed = ["localhost", "127.0.0.1", "[::1]"]
        if _is_wildcard_bind(host):
            # Wildcard bind: the reachable Host header is unknown, so protection
            # stays localhost-only. Remote deployments should terminate at a
            # reverse proxy and set PLAY_STORE_MCP_DISABLE_DNS_REBINDING.
            logger.warning(
                "DNS-rebinding protection allows only localhost on a wildcard bind; "
                "set PLAY_STORE_MCP_DISABLE_DNS_REBINDING=1 for remote access behind a proxy",
                host=host,
            )
        else:
            allowed.append(host)
        middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=allowed))
    if auth_token:
        middleware.append(Middleware(_BearerTokenAuthMiddleware, token=auth_token))
    # transport is constrained to the non-stdio argparse choices ("sse" /
    # "streamable-http"), both valid http_app transports; argparse types it as str.
    app = mcp.http_app(transport=transport, middleware=middleware)  # type: ignore[arg-type]
    uvicorn.run(app, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    """Run the Play Store MCP Server."""
    parser = argparse.ArgumentParser(description="Play Store MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="Transport protocol (default: stdio, or set MCP_TRANSPORT env var)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="Host to bind to for network transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8000")),
        help="Port to bind to for network transports (default: 8000)",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_PLAY_STORE_CREDENTIALS"),
        help="Path to service account JSON key or JSON content (default: GOOGLE_PLAY_STORE_CREDENTIALS env var)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=_env_read_only(),
        help="Disable all write operations (or set PLAY_STORE_MCP_READ_ONLY=1)",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help=(
            "Require 'Authorization: Bearer <token>' on network transports "
            "(prefer the PLAY_STORE_MCP_AUTH_TOKEN env var; CLI args are visible in ps)"
        ),
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        default=False,
        help=(
            "Allow a non-loopback bind without an auth token, e.g. behind an "
            "authenticating proxy (or set PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED=1)"
        ),
    )
    args = parser.parse_args(argv)

    if args.credentials:
        os.environ["GOOGLE_PLAY_STORE_CREDENTIALS"] = args.credentials
    if args.auth_token is not None:
        if not args.auth_token.strip():
            parser.error("--auth-token was given an empty value (auth would silently stay off)")
        os.environ["PLAY_STORE_MCP_AUTH_TOKEN"] = args.auth_token.strip()
    if args.allow_unauthenticated:
        os.environ["PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED"] = "1"

    set_read_only(args.read_only)

    logger.info(
        "Starting Play Store MCP Server",
        transport=args.transport,
        host=args.host if args.transport != "stdio" else None,
        port=args.port if args.transport != "stdio" else None,
        read_only=READ_ONLY,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        _run_http(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
