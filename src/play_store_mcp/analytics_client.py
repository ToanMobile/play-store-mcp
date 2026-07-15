"""Google Analytics Data API (GA4) client (read-only: report + realtime report).

Same google-api-python-client discovery pattern as ReportingClient/
BigQueryClient, scoped to analytics.readonly. Separate client/credential path
from BigQueryClient: GA4 report data is aggregated server-side by Google
Analytics, distinct from the raw per-event rows BigQuery exposes.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from play_store_mcp.client import PlayStoreClientError, _run_with_backoff

if TYPE_CHECKING:
    from googleapiclient._apis.analyticsdata.v1beta import AnalyticsDataResource

logger = structlog.get_logger(__name__)

ANALYTICS_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


class AnalyticsDataClient:
    """Client for the Google Analytics Data API (GA4: runReport / runRealtimeReport)."""

    def __init__(
        self,
        credentials_path: str | None = None,
        credentials_json: str | dict[str, Any] | None = None,
    ) -> None:
        self._credentials_path = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self._credentials_json = credentials_json
        self._service: AnalyticsDataResource | None = None
        self._http_lock = threading.Lock()
        self._logger = logger.bind(component="AnalyticsDataClient")

    def _get_service(self) -> AnalyticsDataResource:
        if self._service is not None:
            return self._service

        self._logger.info("Initializing Analytics Data API client")
        credentials = None
        try:
            if isinstance(self._credentials_json, dict):
                credentials = service_account.Credentials.from_service_account_info(
                    self._credentials_json, scopes=ANALYTICS_SCOPES
                )
            elif not credentials and self._credentials_path:
                creds_path = Path(self._credentials_path)
                if creds_path.exists():
                    credentials = service_account.Credentials.from_service_account_file(
                        str(creds_path), scopes=ANALYTICS_SCOPES
                    )

            if not credentials:
                raise PlayStoreClientError(
                    "No valid credentials found for Analytics Data API. Set GOOGLE_APPLICATION_CREDENTIALS."
                )

            self._service = build(
                "analyticsdata",
                "v1beta",
                credentials=credentials,
                cache_discovery=False,
            )
            self._logger.info("Analytics Data API client initialized successfully")
            return self._service  # type: ignore[return-value]
        except Exception as e:
            if isinstance(e, PlayStoreClientError):
                raise
            self._logger.exception("Failed to initialize Analytics Data API client", error=str(e))
            raise PlayStoreClientError(
                f"Failed to initialize Analytics Data API client: {e}"
            ) from e

    def _execute(self, request: Any) -> Any:
        method = (getattr(request, "method", "") or "").upper()
        retry_server_errors = method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

        def _locked_execute() -> Any:
            with self._http_lock:
                return request.execute()

        return _run_with_backoff(_locked_execute, retry_server_errors=retry_server_errors)

    def run_report(
        self,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        start_date: str = "7daysAgo",
        end_date: str = "today",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Run a GA4 report. Dates accept relative forms like "7daysAgo"/"today" or "YYYY-MM-DD"."""
        service = self._get_service()
        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "limit": limit,
        }
        try:
            return self._execute(
                service.properties().runReport(property=f"properties/{property_id}", body=body)
            )
        except HttpError as e:
            self._logger.exception("runReport failed", error=str(e))
            raise PlayStoreClientError(f"runReport failed: {e.reason}") from e

    def run_realtime_report(
        self,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        limit: int = 100,
    ) -> dict[str, Any]:
        """Run a GA4 realtime report (active users in the last ~30 minutes)."""
        service = self._get_service()
        body = {
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "limit": limit,
        }
        try:
            return self._execute(
                service.properties().runRealtimeReport(
                    property=f"properties/{property_id}", body=body
                )
            )
        except HttpError as e:
            self._logger.exception("runRealtimeReport failed", error=str(e))
            raise PlayStoreClientError(f"runRealtimeReport failed: {e.reason}") from e
