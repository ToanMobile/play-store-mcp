"""Tests for HTTP-transport bearer-token auth and the non-loopback refusal."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from play_store_mcp import server

TOKEN = "s3cret-token-" + "x" * 32


def _capture_run_http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub http_app/uvicorn so _run_http returns immediately; capture its kwargs."""
    captured: dict[str, Any] = {}

    def fake_http_app(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "ASGI_APP"

    class FakeUvicorn:
        @staticmethod
        def run(*_args: Any, **_kwargs: Any) -> None:
            captured["served"] = True

    monkeypatch.setattr(server.mcp, "http_app", fake_http_app)
    monkeypatch.setattr(server, "uvicorn", FakeUvicorn)
    return captured


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("PLAY_STORE_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("PLAY_STORE_MCP_DISABLE_DNS_REBINDING", raising=False)
    monkeypatch.setenv("PLAY_STORE_MCP_DOWNLOAD_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# Startup policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.1.10", "::", "example.com"])  # noqa: S104
def test_non_loopback_without_token_refuses_to_start(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_run_http(monkeypatch)
    with pytest.raises(SystemExit, match="PLAY_STORE_MCP_AUTH_TOKEN"):
        server._run_http("streamable-http", host, 8000)
    assert "served" not in captured


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_without_token_still_starts(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_http(monkeypatch)
    server._run_http("streamable-http", host, 8000)
    assert captured["served"] is True
    classes = [m.cls for m in captured["middleware"]]
    assert server._BearerTokenAuthMiddleware not in classes


def test_non_loopback_with_opt_out_starts_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED", "1")
    captured = _capture_run_http(monkeypatch)
    with patch.object(server.logger, "warning") as mock_warning:
        server._run_http("streamable-http", "0.0.0.0", 8000)  # noqa: S104
    assert captured["served"] is True
    messages = [call.args[0] for call in mock_warning.call_args_list]
    assert any("without authentication" in m for m in messages)


def test_non_loopback_with_token_attaches_auth_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", TOKEN)
    captured = _capture_run_http(monkeypatch)
    server._run_http("sse", "0.0.0.0", 8000)  # noqa: S104
    auth = [m for m in captured["middleware"] if m.cls is server._BearerTokenAuthMiddleware]
    assert len(auth) == 1
    assert auth[0].kwargs == {"token": TOKEN}


def test_auth_middleware_attached_even_when_dns_rebinding_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PLAY_STORE_MCP_DISABLE_DNS_REBINDING", "1")
    captured = _capture_run_http(monkeypatch)
    server._run_http("streamable-http", "10.0.0.5", 8000)
    assert [m.cls for m in captured["middleware"]] == [server._BearerTokenAuthMiddleware]


def test_weak_auth_token_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", "short")
    _capture_run_http(monkeypatch)
    with patch.object(server.logger, "warning") as mock_warning:
        server._run_http("streamable-http", "127.0.0.1", 8000)
    messages = [call.args[0] for call in mock_warning.call_args_list]
    assert any("PLAY_STORE_MCP_AUTH_TOKEN is shorter than recommended" in m for m in messages)


def test_main_flags_set_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() writes os.environ directly; pre-set via monkeypatch so it is restored.
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", "placeholder")
    monkeypatch.setenv("PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED", "0")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(server, "_run_http", lambda *a: calls.update(args=a))
    server.main(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",  # noqa: S104
            "--auth-token",
            TOKEN,
            "--allow-unauthenticated",
        ]
    )
    assert os.environ["PLAY_STORE_MCP_AUTH_TOKEN"] == TOKEN
    assert os.environ["PLAY_STORE_MCP_ALLOW_UNAUTHENTICATED"] == "1"
    assert calls["args"] == ("streamable-http", "0.0.0.0", 8000)  # noqa: S104


# ---------------------------------------------------------------------------
# Middleware behavior
# ---------------------------------------------------------------------------


def _app() -> TestClient:
    async def ok(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/health", ok),
            Route("/credentials", ok, methods=["POST"]),
        ],
        middleware=[Middleware(server._BearerTokenAuthMiddleware, token=TOKEN)],
    )
    return TestClient(app)


def test_middleware_rejects_missing_token() -> None:
    response = _app().post("/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["success"] is False


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {TOKEN}x",
        "Bearer wrong",
        TOKEN,
        f"Basic {TOKEN}",
        "Bearer ",
        # Non-ASCII bytes must yield a 401, not a 500 from compare_digest.
        "Bearer \u00e9".encode("latin-1"),
    ],
)
def test_middleware_rejects_wrong_token(header: str | bytes) -> None:
    client = _app()
    if isinstance(header, bytes):
        response = client.post("/mcp", headers={b"authorization": header})
    else:
        response = client.post("/mcp", headers={"Authorization": header})
    assert response.status_code == 401


def test_middleware_accepts_correct_token() -> None:
    response = _app().post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.text == "ok"


def test_middleware_exempts_health_and_credentials() -> None:
    client = _app()
    assert client.get("/health").status_code == 200
    # /credentials enforces its own PLAY_STORE_MCP_ADMIN_TOKEN check.
    assert client.post("/credentials").status_code == 200


# ---------------------------------------------------------------------------
# Re-audit 2026-09-23
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", [f"bearer {TOKEN}", f"BEARER  {TOKEN} ", f"Bearer {TOKEN}\t"])
def test_middleware_scheme_case_insensitive_and_whitespace_tolerant(header: str) -> None:
    response = _app().post("/mcp", headers={"Authorization": header})
    assert response.status_code == 200


def test_env_token_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", f"{TOKEN}\n")
    assert server._env_token("PLAY_STORE_MCP_AUTH_TOKEN") == TOKEN
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", "   ")
    assert server._env_token("PLAY_STORE_MCP_AUTH_TOKEN") is None


async def test_middleware_closes_websocket_without_token() -> None:
    sent: list[dict[str, Any]] = []
    reached = False

    async def inner(_scope: Any, _receive: Any, _send: Any) -> None:
        nonlocal reached
        reached = True

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    mw = server._BearerTokenAuthMiddleware(inner, token=TOKEN)
    await mw({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)
    assert not reached
    assert sent == [{"type": "websocket.close", "code": 1008}]


def _credentials_request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/credentials",
            "headers": raw,
            "client": ("127.0.0.1", 50000),
        }
    )


def test_credentials_requires_auth_token_even_from_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A same-host tunnel (cloudflared / ngrok / ssh -R) arrives as a loopback peer.
    monkeypatch.delenv("PLAY_STORE_MCP_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("PLAY_STORE_MCP_AUTH_TOKEN", TOKEN)
    denied = server._authorize_credentials_request(_credentials_request())
    assert denied is not None and denied.status_code == 401
    ok = server._authorize_credentials_request(
        _credentials_request({"Authorization": f"Bearer {TOKEN}"})
    )
    assert ok is None


def test_credentials_loopback_only_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAY_STORE_MCP_ADMIN_TOKEN", raising=False)
    assert server._authorize_credentials_request(_credentials_request()) is None


def test_main_rejects_empty_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_run_http", lambda *_a: None)
    with pytest.raises(SystemExit):
        server.main(["--transport", "streamable-http", "--auth-token", ""])


def test_real_http_app_requires_token() -> None:
    app = server.mcp.http_app(
        transport="streamable-http",
        middleware=[Middleware(server._BearerTokenAuthMiddleware, token=TOKEN)],
    )
    with TestClient(app) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/health").status_code == 200


def test_run_http_marks_http_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAY_STORE_MCP_HTTP_MODE", raising=False)
    _capture_run_http(monkeypatch)
    server._run_http("streamable-http", "127.0.0.1", 8000)
    assert os.environ.get("PLAY_STORE_MCP_HTTP_MODE") == "1"
    monkeypatch.delenv("PLAY_STORE_MCP_HTTP_MODE", raising=False)


def test_upload_path_confinement(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from play_store_mcp.client import PlayStoreClient
    from play_store_mcp.errors import PlayStoreClientError

    client = PlayStoreClient(credentials_json={"type": "service_account"})
    inside = tmp_path / "up" / "app.aab"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    # stdio (no HTTP mode, no upload dir): unchanged behavior.
    monkeypatch.delenv("PLAY_STORE_MCP_HTTP_MODE", raising=False)
    monkeypatch.delenv("PLAY_STORE_MCP_UPLOAD_DIR", raising=False)
    assert client._confine_upload_path("/etc/hosts") == "/etc/hosts"
    # HTTP mode without an upload dir: refused.
    monkeypatch.setenv("PLAY_STORE_MCP_HTTP_MODE", "1")
    with pytest.raises(PlayStoreClientError, match="PLAY_STORE_MCP_UPLOAD_DIR"):
        client._confine_upload_path(str(inside))
    # Upload dir set: inside ok, outside / traversal refused.
    monkeypatch.setenv("PLAY_STORE_MCP_UPLOAD_DIR", str(tmp_path / "up"))
    assert client._confine_upload_path(str(inside)) == os.path.realpath(inside)
    for bad in ("/etc/hosts", str(tmp_path / "up" / ".." / "secret")):
        with pytest.raises(PlayStoreClientError, match="inside"):
            client._confine_upload_path(bad)
