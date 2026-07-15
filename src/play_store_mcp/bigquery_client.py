"""Google BigQuery API client (read-only: list datasets/tables, ad-hoc SQL).

Same google-api-python-client discovery pattern as ReportingClient, scoped to
bigquery.readonly so this client can only read, never mutate data.
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
    from googleapiclient._apis.bigquery.v2 import BigqueryResource

logger = structlog.get_logger(__name__)

BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery.readonly"]

# Client-side cost guardrail on top of the read-only scope: a query that would
# scan more than this many bytes fails instead of running.
DEFAULT_MAX_BYTES_BILLED = 1_000_000_000  # 1 GB


class BigQueryClient:
    """Client for the Google BigQuery API (read-only: list/describe/query)."""

    def __init__(
        self,
        credentials_path: str | None = None,
        credentials_json: str | dict[str, Any] | None = None,
    ) -> None:
        self._credentials_path = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self._credentials_json = credentials_json
        self._service: BigqueryResource | None = None
        self._http_lock = threading.Lock()
        self._logger = logger.bind(component="BigQueryClient")

    def _get_service(self) -> BigqueryResource:
        if self._service is not None:
            return self._service

        self._logger.info("Initializing BigQuery API client")
        credentials = None
        try:
            if isinstance(self._credentials_json, dict):
                credentials = service_account.Credentials.from_service_account_info(
                    self._credentials_json, scopes=BIGQUERY_SCOPES
                )
            elif not credentials and self._credentials_path:
                creds_path = Path(self._credentials_path)
                if creds_path.exists():
                    credentials = service_account.Credentials.from_service_account_file(
                        str(creds_path), scopes=BIGQUERY_SCOPES
                    )

            if not credentials:
                raise PlayStoreClientError(
                    "No valid credentials found for BigQuery API. Set GOOGLE_APPLICATION_CREDENTIALS."
                )

            self._service = build(
                "bigquery",
                "v2",
                credentials=credentials,
                cache_discovery=False,
            )
            self._logger.info("BigQuery API client initialized successfully")
            return self._service  # type: ignore[return-value]
        except Exception as e:
            if isinstance(e, PlayStoreClientError):
                raise
            self._logger.exception("Failed to initialize BigQuery API client", error=str(e))
            raise PlayStoreClientError(f"Failed to initialize BigQuery API client: {e}") from e

    def _execute(self, request: Any) -> Any:
        method = (getattr(request, "method", "") or "").upper()
        retry_server_errors = method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

        def _locked_execute() -> Any:
            with self._http_lock:
                return request.execute()

        return _run_with_backoff(_locked_execute, retry_server_errors=retry_server_errors)

    def list_datasets(self, project_id: str) -> dict[str, Any]:
        service = self._get_service()
        try:
            return self._execute(service.datasets().list(projectId=project_id))
        except HttpError as e:
            self._logger.exception("datasets.list failed", error=str(e))
            raise PlayStoreClientError(f"Failed to list datasets: {e.reason}") from e

    def list_tables(
        self, project_id: str, dataset_id: str, max_results: int = 50
    ) -> dict[str, Any]:
        service = self._get_service()
        try:
            return self._execute(
                service.tables().list(
                    projectId=project_id, datasetId=dataset_id, maxResults=max_results
                )
            )
        except HttpError as e:
            self._logger.exception("tables.list failed", error=str(e))
            raise PlayStoreClientError(f"Failed to list tables: {e.reason}") from e

    def get_table_schema(self, project_id: str, dataset_id: str, table_id: str) -> dict[str, Any]:
        service = self._get_service()
        try:
            table = self._execute(
                service.tables().get(projectId=project_id, datasetId=dataset_id, tableId=table_id)
            )
        except HttpError as e:
            self._logger.exception("tables.get failed", error=str(e))
            raise PlayStoreClientError(f"Failed to get table schema: {e.reason}") from e
        return {
            "tableId": table_id,
            "numRows": table.get("numRows"),
            "numBytes": table.get("numBytes"),
            "schema": table.get("schema", {}).get("fields", []),
        }

    def execute_query(
        self,
        project_id: str,
        query: str,
        max_results: int = 100,
        max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run a read-only SQL query (standard SQL).

        The bigquery.readonly scope on this client's credentials already
        blocks INSERT/UPDATE/DELETE/DDL server-side; max_bytes_billed is an
        additional client-side cost guardrail on top of that.
        """
        service = self._get_service()
        body = {
            "query": query,
            "useLegacySql": False,
            "maxResults": max_results,
            "maximumBytesBilled": str(max_bytes_billed),
            "dryRun": dry_run,
        }
        try:
            return self._execute(service.jobs().query(projectId=project_id, body=body))
        except HttpError as e:
            self._logger.exception("jobs.query failed", error=str(e))
            raise PlayStoreClientError(f"Query failed: {e.reason}") from e
