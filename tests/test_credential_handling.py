"""Regression tests for the credential-handling audit fixes.

Each test here pins a security property that was violated before, not merely
an implementation detail:

1. A failed credential load must not print the private key into the logs.
2. Every per-request client resolver must honour the credential headers.
3. Rotating credentials must replace every cached client.
4. All clients must accept the same credential forms (JSON content or a path).
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from starlette.requests import Request

from play_store_mcp import server
from play_store_mcp.analytics_client import AnalyticsDataClient
from play_store_mcp.bigquery_client import BigQueryClient
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError
from play_store_mcp.crashlytics_client import CrashlyticsClient
from play_store_mcp.credentials import load_service_account_credentials
from play_store_mcp.reporting_client import ReportingClient

KEY_MARKER = "SUPERSECRETKEYMATERIAL"  # nosec B105 — test sentinel, not a credential

CREDENTIALS = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "abc123",
    "private_key": f"-----BEGIN PRIVATE KEY-----\n{KEY_MARKER}\n-----END PRIVATE KEY-----\n",
    "client_email": "sa@test-project.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


# --------------------------------------------------------------------------
# 1. Private key must never reach the logs
# --------------------------------------------------------------------------


def _capture_init_failure_log() -> str:
    """Trigger a credential-init failure and return everything that was logged.

    The private key above is not a real PEM, so google-auth raises while
    building the signer — with the service account dict live in the frame
    locals of several stack frames.
    """
    buf = io.StringIO()
    # Reuse the processors server.py configured — the point is to exercise the
    # real renderer — and only redirect the sink. Restore afterwards rather than
    # reset_defaults(), which would drop that configuration and leave later
    # tests inspecting structlog's own (leaky) defaults.
    saved = structlog.get_config()
    structlog.configure(
        processors=saved["processors"],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )
    try:
        with pytest.raises(PlayStoreClientError):
            CrashlyticsClient(credentials_json=json.dumps(CREDENTIALS))._get_service()
        return buf.getvalue()
    finally:
        structlog.configure(**saved)


def test_failed_credential_init_does_not_log_private_key() -> None:
    output = _capture_init_failure_log()

    assert KEY_MARKER not in output
    assert "BEGIN PRIVATE KEY" not in output


def test_failed_credential_init_still_logs_a_usable_diagnostic() -> None:
    """Redaction must not cost us the ability to debug: the error still surfaces."""
    output = _capture_init_failure_log()

    assert "Failed to initialize Firebase Crashlytics API client" in output


def test_server_log_renderer_disables_show_locals() -> None:
    """The renderer configured at import time is the one that must be safe.

    _capture_init_failure_log builds its own renderer, so without this the
    suite would pass even if server.py reverted to the leaky default.
    """
    renderer = next(
        p
        for p in structlog.get_config()["processors"]
        if isinstance(p, structlog.dev.ConsoleRenderer)
    )
    formatter = renderer._exception_formatter

    assert isinstance(formatter, structlog.dev.RichTracebackFormatter)
    assert formatter.show_locals is False


# --------------------------------------------------------------------------
# 2. Every resolver honours the per-request credential headers
# --------------------------------------------------------------------------

RESOLVERS: list[tuple[str, Any]] = [
    ("get_client_from_context", PlayStoreClient),
    ("get_crashlytics_client_from_context", CrashlyticsClient),
    ("get_reporting_client_from_context", ReportingClient),
    ("get_bigquery_client_from_context", BigQueryClient),
    ("get_analytics_client_from_context", AnalyticsDataClient),
]


@pytest.mark.parametrize(("resolver_name", "expected_type"), RESOLVERS)
def test_resolver_uses_per_request_credentials(
    resolver_name: str, expected_type: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver that ignored the header would hand back the server's own client.

    In a multi-tenant HTTP deployment that means tenant A's request executing
    against the server's ambient Google identity.
    """
    ambient = MagicMock()
    monkeypatch.setattr(
        server,
        "_shared_state",
        dict.fromkeys(
            ["client", "crashlytics_client", "reporting_client", "bigquery_client"], ambient
        )
        | {"analytics_client": ambient},
    )
    with patch(
        "play_store_mcp.server.get_http_headers",
        return_value={"x-google-credentials": json.dumps(CREDENTIALS)},
    ):
        client = getattr(server, resolver_name)()

    assert client is not ambient
    assert isinstance(client, expected_type)
    assert client._credentials_json == CREDENTIALS


@pytest.mark.parametrize(("resolver_name", "expected_type"), RESOLVERS)
def test_resolver_falls_back_to_shared_client_without_headers(
    resolver_name: str,
    expected_type: type,  # noqa: ARG001 — parametrized in tandem with the test above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = MagicMock()
    monkeypatch.setattr(
        server,
        "_shared_state",
        dict.fromkeys(
            ["client", "crashlytics_client", "reporting_client", "bigquery_client"], ambient
        )
        | {"analytics_client": ambient},
    )
    with patch("play_store_mcp.server.get_http_headers", return_value={}):
        assert getattr(server, resolver_name)() is ambient


# --------------------------------------------------------------------------
# 3. Rotation replaces every cached client
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_rotation_replaces_every_cached_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation is usually a response to a leak, so no client may keep the old key."""
    stale = MagicMock()
    cached = [
        "client",
        "crashlytics_client",
        "reporting_client",
        "bigquery_client",
        "analytics_client",
    ]
    monkeypatch.setattr(
        server, "_shared_state", dict.fromkeys(cached, stale) | {"credentials_updated": False}
    )

    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    request.json = AsyncMock(return_value={"credentials": CREDENTIALS})

    with patch("play_store_mcp.client.PlayStoreClient._get_service", return_value=MagicMock()):
        response = await server.update_credentials(request)

    assert response.status_code == 200
    for name in cached:
        assert server._shared_state[name] is not stale, f"{name} kept the rotated-out credentials"
        assert server._shared_state[name]._credentials_json == CREDENTIALS


# --------------------------------------------------------------------------
# 4. One credential loader, same accepted forms everywhere
# --------------------------------------------------------------------------

CLIENTS = [PlayStoreClient, CrashlyticsClient, ReportingClient, BigQueryClient, AnalyticsDataClient]


@pytest.mark.parametrize("client_cls", CLIENTS)
def test_every_client_accepts_a_path_in_the_shared_env_var(
    client_cls: type, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--credentials documents "path or JSON content"; every client must honour both."""
    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps(CREDENTIALS))
    monkeypatch.setenv("GOOGLE_PLAY_STORE_CREDENTIALS", str(key_file))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with patch(
        "play_store_mcp.credentials.service_account.Credentials.from_service_account_info"
    ) as from_file:
        from_file.return_value = MagicMock()
        client_cls()._get_service()

    # Every branch now funnels through from_service_account_info (single
    # token_uri choke point), so the path is read and its parsed content is
    # what reaches google-auth.
    assert from_file.call_args.args[0] == CREDENTIALS


@pytest.mark.parametrize("client_cls", CLIENTS)
def test_every_client_accepts_inline_json_in_the_shared_env_var(
    client_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLAY_STORE_CREDENTIALS", json.dumps(CREDENTIALS))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with patch(
        "play_store_mcp.credentials.service_account.Credentials.from_service_account_info"
    ) as from_info:
        from_info.return_value = MagicMock()
        client_cls()._get_service()

    assert from_info.call_args.args[0] == CREDENTIALS


def test_loader_reports_missing_credentials_with_both_env_vars_named() -> None:
    with pytest.raises(PlayStoreClientError, match="GOOGLE_PLAY_STORE_CREDENTIALS"):
        load_service_account_credentials(
            credentials_json=None,
            credentials_path=None,
            scopes=["https://www.googleapis.com/auth/firebase"],
            api_label="Test API",
        )
