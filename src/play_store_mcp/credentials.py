"""Shared service-account credential loading for every Google API client.

Each client authenticates with the same service account but a different OAuth
scope, and every one of them used to re-implement the "is this JSON content or
a file path?" resolution. The copies drifted: only ``PlayStoreClient`` accepted
a path in ``GOOGLE_PLAY_STORE_CREDENTIALS``, while the others either ignored
the variable or fed the path straight to ``json.loads``. This module is the one
implementation they all share.

Nothing here logs. Credential material must never reach a log record, and the
callers wrap failures in ``PlayStoreClientError`` whose message carries only
the API label and the underlying library's complaint (missing field names, bad
PEM padding) — never the key itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from google.oauth2 import service_account

from play_store_mcp.errors import PlayStoreClientError

if TYPE_CHECKING:
    from collections.abc import Sequence

# The only token endpoint a Google service-account key may name. A key file
# is normally trusted, but on the HTTP transport a per-request header can carry
# credentials from a caller, who could otherwise point ``token_uri`` at an
# arbitrary URL and have this server POST the signed JWT there (SSRF / token
# theft). Merged from upstream lusky3/play-store-mcp #147 on 2026-09-14.
GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 # nosec B105 — endpoint URL, not a credential

CREDENTIAL_ENV_HINT = (
    "Set GOOGLE_APPLICATION_CREDENTIALS (path to the service account JSON key) "
    "or GOOGLE_PLAY_STORE_CREDENTIALS (the JSON content itself, or a path to it)."
)


def _from_info(
    info: dict[str, Any], scopes: list[str], api_label: str
) -> service_account.Credentials:
    """Build credentials from parsed key JSON, rejecting a spoofed ``token_uri``.

    Every branch (dict, JSON string, file path) funnels through here so the
    ``token_uri`` check is one universal choke point.
    """
    token_uri = info.get("token_uri")
    if token_uri is not None and token_uri != GOOGLE_OAUTH_TOKEN_URI:
        raise PlayStoreClientError(
            f"Invalid token_uri in service account credentials for {api_label}: {token_uri!r} "
            f"(expected {GOOGLE_OAUTH_TOKEN_URI!r})"
        )
    return cast(
        "service_account.Credentials",
        service_account.Credentials.from_service_account_info(info, scopes=scopes),
    )


def _from_file(path: str, scopes: list[str], api_label: str) -> service_account.Credentials:
    try:
        with Path(path).open(encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError) as e:
        # Wrapped so a bad key file is a logged startup warning, not a crash.
        raise PlayStoreClientError(
            f"Cannot read {api_label} credentials file {path}: {type(e).__name__}"
        ) from e
    return _from_info(info, scopes, api_label)


def load_service_account_credentials(
    *,
    credentials_json: str | dict[str, Any] | None,
    credentials_path: str | None,
    scopes: Sequence[str],
    api_label: str,
) -> service_account.Credentials:
    """Resolve service account credentials for ``api_label`` at ``scopes``.

    ``credentials_json`` accepts a parsed dict, a JSON string, or a path to a
    JSON key file; ``credentials_path`` is the fallback and is always a path.

    Raises:
        PlayStoreClientError: if nothing usable was configured, or if the value
            looks like JSON but does not parse.
    """
    scopes = list(scopes)

    if isinstance(credentials_json, dict):
        return _from_info(credentials_json, scopes, api_label)

    if isinstance(credentials_json, str) and credentials_json.strip():
        value = credentials_json.strip()
        if value.startswith("{"):
            try:
                info = json.loads(value)
            except json.JSONDecodeError as e:
                # Deliberately does not echo the value: on a truncated key file
                # the fragment would be key material.
                raise PlayStoreClientError(
                    f"Credentials for {api_label} look like JSON but failed to parse "
                    f"({e.msg} at line {e.lineno} column {e.colno})."
                ) from e
            return _from_info(info, scopes, api_label)
        if Path(value).exists():
            return _from_file(value, scopes, api_label)

    if credentials_path and Path(credentials_path).exists():
        return _from_file(credentials_path, scopes, api_label)

    raise PlayStoreClientError(f"No valid credentials found for {api_label}. {CREDENTIAL_ENV_HINT}")
