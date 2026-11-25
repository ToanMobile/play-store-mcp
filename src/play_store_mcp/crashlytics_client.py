"""Firebase Crashlytics API client for issue state management."""

from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from play_store_mcp.client import PlayStoreClientError, _run_with_backoff
from play_store_mcp.credentials import load_service_account_credentials

logger = structlog.get_logger(__name__)

CRASHLYTICS_SCOPES = ["https://www.googleapis.com/auth/firebase"]

# Crashlytics issue IDs are 32 lowercase hex characters. The API answers an
# unknown or truncated ID with 500 INTERNAL rather than 404, so validating the
# shape here turns an opaque "Internal error encountered." into a clear message.
_ISSUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Crashlytics issue IDs are not Play Developer Reporting (Android Vitals) issue
# IDs, even though both are 32-character hex: the two products track the same
# crash under different identifiers. Feeding a Vitals ID to this API is exactly
# the "valid shape, unknown issue" case that answers 500 INTERNAL, which is why
# list_issues exists — it is the only way to obtain an ID this API accepts.
TOP_ISSUES_REPORT = "topIssues"

ERROR_TYPES = ("FATAL", "NON_FATAL", "ANR")
ISSUE_STATES = ("OPEN", "CLOSED", "MUTED")


def _normalized_segments(segments: dict[str, str]) -> dict[str, str]:
    """Validate resource-name path segments (non-empty, not a nested path)."""
    normalized: dict[str, str] = {}
    for field, value in segments.items():
        value = value.strip()
        if not value:
            raise PlayStoreClientError(f"{field} must not be empty")
        if "/" in value:
            raise PlayStoreClientError(f"{field} must be an ID, not a resource path containing '/'")
        normalized[field] = value
    return normalized


def _app_resource_name(project_id: str, app_id: str) -> str:
    """Build a Crashlytics app resource name from validated path segments."""
    normalized = _normalized_segments({"project_id": project_id, "app_id": app_id})
    return f"projects/{normalized['project_id']}/apps/{normalized['app_id']}"


def _issue_resource_name(project_id: str, app_id: str, issue_id: str) -> str:
    """Build a Crashlytics issue resource name from validated path segments."""
    normalized = _normalized_segments(
        {
            "project_id": project_id,
            "app_id": app_id,
            "issue_id": issue_id,
        }
    )

    if not _ISSUE_ID_RE.match(normalized["issue_id"]):
        raise PlayStoreClientError(
            "issue_id must be a full 32-character lowercase hex Crashlytics issue ID "
            f"(for example, c07d6e046632025ecd72f628ee1bf2ce), got {normalized['issue_id']!r}"
        )

    return (
        f"projects/{normalized['project_id']}/apps/{normalized['app_id']}"
        f"/issues/{normalized['issue_id']}"
    )


def _rfc3339(dt: datetime) -> str:
    """Format a datetime the way the API's google-datetime fields expect."""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validated_enum(field: str, value: str, allowed: tuple[str, ...]) -> str:
    """Uppercase and check an enum filter value before it reaches the API."""
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise PlayStoreClientError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")
    return normalized


def _summarize_issue_group(group: dict[str, Any]) -> dict[str, Any]:
    """Flatten one topIssues ReportGroup into a flat issue record.

    The issue's ``id`` is the value ``close_issue`` needs; the full resource
    ``name`` is kept alongside it so the caller can see which app it came from.
    """
    issue = group.get("issue") or {}
    events = 0
    impacted_users = 0
    for interval in group.get("metrics") or []:
        events += int(interval.get("eventsCount") or 0)
        impacted_users += int(interval.get("impactedUsersCount") or 0)

    return {
        "issueId": issue.get("id"),
        "name": issue.get("name"),
        "title": issue.get("title"),
        "subtitle": issue.get("subtitle"),
        "errorType": issue.get("errorType"),
        "state": issue.get("state"),
        "firstSeenVersion": issue.get("firstSeenVersion"),
        "lastSeenVersion": issue.get("lastSeenVersion"),
        "firstSeenTime": issue.get("firstSeenTime"),
        "lastSeenTime": issue.get("lastSeenTime"),
        "signals": [s.get("signal") for s in issue.get("signals") or []],
        "eventsCount": events,
        "impactedUsersCount": impacted_users,
        "uri": issue.get("uri"),
    }


class CrashlyticsClient:
    """Client for the Firebase Crashlytics API."""

    def __init__(
        self,
        credentials_path: str | None = None,
        credentials_json: str | dict[str, Any] | None = None,
    ) -> None:
        self._credentials_path = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self._credentials_json = credentials_json or os.environ.get("GOOGLE_PLAY_STORE_CREDENTIALS")
        self._service: Any = None
        self._http_lock = threading.Lock()
        self._logger = logger.bind(component="CrashlyticsClient")

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        self._logger.info("Initializing Firebase Crashlytics API client")
        try:
            credentials = load_service_account_credentials(
                credentials_json=self._credentials_json,
                credentials_path=self._credentials_path,
                scopes=CRASHLYTICS_SCOPES,
                api_label="Firebase Crashlytics API",
            )

            # static_discovery=False is required: google-api-python-client only
            # ships bundled discovery documents for a subset of APIs, and
            # firebasecrashlytics v1alpha is not one of them. With the 2.x
            # default (static_discovery=True) build() raises
            # UnknownApiNameOrVersion before any request is made.
            self._service = build(
                "firebasecrashlytics",
                "v1alpha",
                credentials=credentials,
                cache_discovery=False,
                static_discovery=False,
            )
            self._logger.info("Firebase Crashlytics API client initialized successfully")
            return self._service
        except Exception as e:
            if isinstance(e, PlayStoreClientError):
                raise
            self._logger.exception(
                "Failed to initialize Firebase Crashlytics API client",
                error=str(e),
            )
            raise PlayStoreClientError(
                f"Failed to initialize Firebase Crashlytics API client: {e}"
            ) from e

    def _execute(self, request: Any) -> Any:
        def _locked_execute() -> Any:
            with self._http_lock:
                return request.execute()

        # The patch is idempotent, but the Crashlytics API answers a valid-looking
        # yet unknown issue ID with 500 INTERNAL. Retrying that just burns the
        # whole backoff before reporting a failure that will never succeed, so
        # fail fast on server errors and let 429s still be retried.
        return _run_with_backoff(_locked_execute, retry_server_errors=False)

    def list_issues(
        self,
        project_id: str,
        app_id: str,
        days: int = 30,
        error_type: str = "",
        state: str = "",
        search: str = "",
        page_size: int = 25,
        page_token: str = "",
    ) -> dict[str, Any]:
        """List Crashlytics issues, newest events first, via the topIssues report.

        The v1alpha API exposes no ``issues.list`` method; the ``topIssues``
        report is the supported way to enumerate issues, and it is the only
        source of issue IDs that ``close_issue`` and ``get_issue`` accept.
        """
        parent = _app_resource_name(project_id, app_id)
        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "name": f"{parent}/reports/{TOP_ISSUES_REPORT}",
            "pageSize": max(1, min(page_size, 100)),
            "filter_interval_startTime": _rfc3339(now - timedelta(days=max(1, min(days, 365)))),
            "filter_interval_endTime": _rfc3339(now),
        }
        if error_type:
            params["filter_issue_errorTypes"] = [
                _validated_enum("error_type", error_type, ERROR_TYPES)
            ]
        if state:
            params["filter_issue_states"] = [_validated_enum("state", state, ISSUE_STATES)]
        if search:
            params["filter_issue_content"] = search
        if page_token:
            params["pageToken"] = page_token

        service = self._get_service()
        try:
            report = cast(
                "dict[str, Any]",
                self._execute(service.projects().apps().reports().get(**params)),
            )
        except HttpError as e:
            self._logger.exception(
                "Crashlytics issue listing failed",
                report_name=params["name"],
                error=str(e),
            )
            raise PlayStoreClientError(
                f"Failed to list Firebase Crashlytics issues: {e.reason}"
            ) from e

        issues = [_summarize_issue_group(group) for group in report.get("groups") or []]
        return {
            "app": parent,
            "periodDays": days,
            "issues": issues,
            "totalIssues": len(issues),
            "totalAvailable": report.get("totalSize"),
            "nextPageToken": report.get("nextPageToken"),
        }

    def get_issue(
        self,
        project_id: str,
        app_id: str,
        issue_id: str,
    ) -> dict[str, Any]:
        """Fetch one Crashlytics issue, to confirm an ID before closing it.

        Like ``close_issue``, an ID that is well-formed but unknown to
        Crashlytics answers 500 INTERNAL rather than 404.
        """
        name = _issue_resource_name(project_id, app_id, issue_id)
        service = self._get_service()
        try:
            return cast(
                "dict[str, Any]",
                self._execute(service.projects().apps().issues().get(name=name)),
            )
        except HttpError as e:
            self._logger.exception(
                "Crashlytics issue lookup failed",
                issue_name=name,
                error=str(e),
            )
            raise PlayStoreClientError(
                f"Failed to get Firebase Crashlytics issue: {e.reason}"
            ) from e

    def close_issue(
        self,
        project_id: str,
        app_id: str,
        issue_id: str,
    ) -> dict[str, Any]:
        """Close a Firebase Crashlytics crash, non-fatal, or ANR issue.

        ``issue_id`` must be the full 32-character lowercase hex issue ID; the
        API answers a truncated or unknown ID with 500 INTERNAL, not 404.
        """
        name = _issue_resource_name(project_id, app_id, issue_id)
        service = self._get_service()
        try:
            return cast(
                "dict[str, Any]",
                self._execute(
                    service.projects()
                    .apps()
                    .issues()
                    .patch(
                        name=name,
                        updateMask="state",
                        body={"name": name, "state": "CLOSED"},
                    )
                ),
            )
        except HttpError as e:
            self._logger.exception(
                "Crashlytics issue close failed",
                issue_name=name,
                error=str(e),
            )
            raise PlayStoreClientError(
                f"Failed to close Firebase Crashlytics issue: {e.reason}"
            ) from e
