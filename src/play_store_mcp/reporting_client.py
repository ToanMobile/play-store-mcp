"""Google Play Developer Reporting API client (Android Vitals: crash/ANR).

Adapted from the crash/ANR query logic in AgiMaulana/GooglePlayConsoleMcp
(MIT licensed), rewired onto this project's PlayStoreClientError/backoff/
service-caching conventions instead of a bare AuthorizedSession.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from play_store_mcp.client import PlayStoreClientError, _run_with_backoff

if TYPE_CHECKING:
    from googleapiclient._apis.playdeveloperreporting.v1beta1 import (
        PlayDeveloperReportingResource,
    )

logger = structlog.get_logger(__name__)

REPORTING_SCOPES = ["https://www.googleapis.com/auth/playdeveloperreporting"]


def _date_parts(dt: datetime) -> dict[str, int]:
    return {"year": dt.year, "month": dt.month, "day": dt.day}


def _parse_reporting_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the timelineSpec dimension/metric rows returned by a vitals query."""
    parsed = []
    for row in rows:
        dims = {
            d.get("dimension"): d.get("stringValue") or d.get("int64Value")
            for d in row.get("dimensions", [])
        }
        metrics: dict[str, Any] = {}
        for m in row.get("metrics", []):
            name = m.get("metric")
            val = m.get("decimalValue") or m.get("int64Value")
            if isinstance(val, dict):
                val = val.get("value")
            if val is not None:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    val = None
            metrics[name] = val
        parsed.append({"date": row.get("startTime", {}), "versionCode": dims.get("versionCode"), **metrics})
    return parsed


class ReportingClient:
    """Client for the Google Play Developer Reporting API (Android Vitals)."""

    def __init__(
        self,
        credentials_path: str | None = None,
        credentials_json: str | dict[str, Any] | None = None,
    ) -> None:
        self._credentials_path = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        self._credentials_json = credentials_json
        self._service: PlayDeveloperReportingResource | None = None
        self._http_lock = threading.Lock()
        self._logger = logger.bind(component="ReportingClient")

    def _get_service(self) -> PlayDeveloperReportingResource:
        if self._service is not None:
            return self._service

        self._logger.info("Initializing Play Developer Reporting API client")
        credentials = None
        try:
            if isinstance(self._credentials_json, dict):
                credentials = service_account.Credentials.from_service_account_info(
                    self._credentials_json, scopes=REPORTING_SCOPES
                )
            elif not credentials and self._credentials_path:
                creds_path = Path(self._credentials_path)
                if creds_path.exists():
                    credentials = service_account.Credentials.from_service_account_file(
                        str(creds_path), scopes=REPORTING_SCOPES
                    )

            if not credentials:
                raise PlayStoreClientError(
                    "No valid credentials found for Reporting API. Set GOOGLE_APPLICATION_CREDENTIALS."
                )

            self._service = build(
                "playdeveloperreporting",
                "v1beta1",
                credentials=credentials,
                cache_discovery=False,
            )
            self._logger.info("Reporting API client initialized successfully")
            return self._service  # type: ignore[return-value]
        except Exception as e:
            if isinstance(e, PlayStoreClientError):
                raise
            self._logger.exception("Failed to initialize Reporting API client", error=str(e))
            raise PlayStoreClientError(f"Failed to initialize Reporting API client: {e}") from e

    def _execute(self, request: Any) -> Any:
        method = (getattr(request, "method", "") or "").upper()
        retry_server_errors = method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

        def _locked_execute() -> Any:
            with self._http_lock:
                return request.execute()

        return _run_with_backoff(_locked_execute, retry_server_errors=retry_server_errors)

    # (Python attribute name on `vitals()`, camelCase MetricSet resource id)
    _METRIC_SETS = {
        "crashrate": "crashRateMetricSet",
        "anrrate": "anrRateMetricSet",
        "stuckbackgroundwakelockrate": "stuckBackgroundWakelockRateMetricSet",
        "excessivewakeuprate": "excessiveWakeupRateMetricSet",
    }

    def _query_metric_set(
        self,
        package_name: str,
        metric_set: str,
        metrics: list[str],
        days: int,
        version_code: str | None = None,
    ) -> dict[str, Any]:
        # Vitals data lags ~1 day; using today as endTime returns 400.
        end = datetime.now(UTC) - timedelta(days=1)
        start = end - timedelta(days=days)
        body: dict[str, Any] = {
            "timelineSpec": {
                "aggregationPeriod": "DAILY",
                "startTime": _date_parts(start),
                "endTime": _date_parts(end),
            },
            "metrics": metrics,
            "dimensions": ["versionCode"],
            "pageSize": days * 10,
        }
        if version_code:
            body["filter"] = f'versionCode = "{version_code}"'

        service = self._get_service()
        resource = getattr(service.vitals(), metric_set)()
        resource_id = self._METRIC_SETS[metric_set]
        try:
            return self._execute(resource.query(name=f"apps/{package_name}/{resource_id}", body=body))
        except HttpError as e:
            self._logger.exception("Vitals query failed", metric_set=metric_set, error=str(e))
            raise PlayStoreClientError(f"Failed to query {metric_set}: {e.reason}") from e

    def query_crash_rate(self, package_name: str, days: int = 7, version_code: str | None = None) -> dict[str, Any]:
        return self._query_metric_set(
            package_name, "crashrate", ["crashRate", "userPerceivedCrashRate", "distinctUsers"], days, version_code
        )

    def query_anr_rate(self, package_name: str, days: int = 7, version_code: str | None = None) -> dict[str, Any]:
        return self._query_metric_set(
            package_name, "anrrate", ["anrRate", "userPerceivedAnrRate", "distinctUsers"], days, version_code
        )

    def query_wakelock_rate(self, package_name: str, days: int = 7, version_code: str | None = None) -> dict[str, Any]:
        return self._query_metric_set(
            package_name, "stuckbackgroundwakelockrate", ["stuckBackgroundWakelockRate", "distinctUsers"], days, version_code
        )

    def query_wakeup_rate(self, package_name: str, days: int = 7, version_code: str | None = None) -> dict[str, Any]:
        return self._query_metric_set(
            package_name, "excessivewakeuprate", ["excessiveWakeupRate", "distinctUsers"], days, version_code
        )

    def search_error_issues(
        self,
        package_name: str,
        days: int = 30,
        issue_type: str | None = None,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List error issues (CRASH/ANR/NON_FATAL) with reports in the last ``days``.

        The Reporting API has no "open/closed" issue status — an issue only
        appears here if it has at least one report inside the requested window.
        """
        end = datetime.now(UTC) - timedelta(days=1)
        start = end - timedelta(days=days)
        kwargs: dict[str, Any] = {
            "parent": f"apps/{package_name}",
            "interval_startTime_year": start.year,
            "interval_startTime_month": start.month,
            "interval_startTime_day": start.day,
            "interval_endTime_year": end.year,
            "interval_endTime_month": end.month,
            "interval_endTime_day": end.day,
            "pageSize": page_size,
            "orderBy": "errorReportCount desc",
        }
        if issue_type:
            kwargs["filter"] = f"errorIssueType = {issue_type}"

        service = self._get_service()
        try:
            return self._execute(service.vitals().errors().issues().search(**kwargs))
        except HttpError as e:
            self._logger.exception("errorIssues.search failed", error=str(e))
            raise PlayStoreClientError(f"Failed to search error issues: {e.reason}") from e

    def search_error_reports(
        self,
        package_name: str,
        days: int = 30,
        issue_id: str | None = None,
        issue_type: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Search raw error reports (includes full stack trace in ``reportText``).

        Unlike search_error_issues (grouped summary), this returns individual
        reports — pass issue_id (from an ErrorIssue's ``name``) to fetch the
        reports behind one specific issue.
        """
        end = datetime.now(UTC) - timedelta(days=1)
        start = end - timedelta(days=days)
        filters = []
        if issue_id:
            filters.append(f"errorIssueId = {issue_id}")
        if issue_type:
            filters.append(f"errorIssueType = {issue_type}")

        kwargs: dict[str, Any] = {
            "parent": f"apps/{package_name}",
            "interval_startTime_year": start.year,
            "interval_startTime_month": start.month,
            "interval_startTime_day": start.day,
            "interval_endTime_year": end.year,
            "interval_endTime_month": end.month,
            "interval_endTime_day": end.day,
            "pageSize": page_size,
        }
        if filters:
            kwargs["filter"] = " AND ".join(filters)

        service = self._get_service()
        try:
            return self._execute(service.vitals().errors().reports().search(**kwargs))
        except HttpError as e:
            self._logger.exception("errorReports.search failed", error=str(e))
            raise PlayStoreClientError(f"Failed to search error reports: {e.reason}") from e
