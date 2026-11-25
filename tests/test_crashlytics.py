"""Tests for Firebase Crashlytics issue state management."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import play_store_mcp.server as server
from play_store_mcp.client import PlayStoreClientError
from play_store_mcp.crashlytics_client import (
    CRASHLYTICS_SCOPES,
    CrashlyticsClient,
    _issue_resource_name,
)


def _make_http_error(reason: str = "boom") -> HttpError:
    response = MagicMock()
    response.status = 400
    response.reason = reason
    error = HttpError(response, b"{}")
    error.reason = reason
    return error


def _issues(service: MagicMock) -> MagicMock:
    return service.projects.return_value.apps.return_value.issues.return_value


def _reports(service: MagicMock) -> MagicMock:
    return service.projects.return_value.apps.return_value.reports.return_value


ISSUE_ID = "c07d6e046632025ecd72f628ee1bf2ce"
ISSUE_NAME = f"projects/my-project/apps/1:123:android:abc/issues/{ISSUE_ID}"
APP_NAME = "projects/my-project/apps/1:123:android:abc"


def test_issue_resource_name() -> None:
    assert _issue_resource_name("my-project", "1:123:android:abc", ISSUE_ID) == ISSUE_NAME


@pytest.mark.parametrize(
    ("field_value", "message"),
    [
        ("", "must not be empty"),
        ("projects/p", "must be an ID"),
    ],
)
def test_issue_resource_name_rejects_invalid_segments(
    field_value: str,
    message: str,
) -> None:
    with pytest.raises(PlayStoreClientError, match=message):
        _issue_resource_name(field_value, "app", ISSUE_ID)


@pytest.mark.parametrize(
    "issue_id",
    [
        "c07d6e04",  # truncated prefix
        f"{ISSUE_ID}0",  # too long
        "C07D6E046632025ECD72F628EE1BF2CE",  # uppercase
        "g07d6e046632025ecd72f628ee1bf2cz",  # non-hex characters
        "issue-42",
    ],
)
def test_issue_resource_name_rejects_malformed_issue_id(issue_id: str) -> None:
    """The API answers a malformed issue ID with 500 INTERNAL, so reject it locally."""
    with pytest.raises(PlayStoreClientError, match="32-character lowercase hex"):
        _issue_resource_name("my-project", "1:123:android:abc", issue_id)


def _client_with(service: MagicMock) -> CrashlyticsClient:
    client = CrashlyticsClient(credentials_json={"type": "service_account"})
    client._service = service
    return client


def test_list_issues_flattens_top_issues_report() -> None:
    service = MagicMock()
    _reports(service).get.return_value.execute.return_value = {
        "totalSize": 42,
        "nextPageToken": "next",
        "groups": [
            {
                "issue": {
                    "id": ISSUE_ID,
                    "name": ISSUE_NAME,
                    "title": "MainTabsScreen.kt",
                    "subtitle": "BadParcelableException",
                    "errorType": "FATAL",
                    "state": "OPEN",
                    "lastSeenVersion": "1.2.3 (456)",
                    "signals": [{"signal": "SIGNAL_FRESH"}],
                    "uri": "https://console.firebase.google.com/issue",
                },
                # Without a granularity the report still returns a list, so the
                # counts have to be summed rather than read off metrics[0].
                "metrics": [
                    {"eventsCount": "10", "impactedUsersCount": "4"},
                    {"eventsCount": "5", "impactedUsersCount": "2"},
                ],
            }
        ],
    }

    result = _client_with(service).list_issues("my-project", "1:123:android:abc")

    assert result["totalIssues"] == 1
    assert result["totalAvailable"] == 42
    assert result["nextPageToken"] == "next"
    issue = result["issues"][0]
    assert issue["issueId"] == ISSUE_ID
    assert issue["subtitle"] == "BadParcelableException"
    assert issue["signals"] == ["SIGNAL_FRESH"]
    assert issue["eventsCount"] == 15
    assert issue["impactedUsersCount"] == 6

    params = _reports(service).get.call_args.kwargs
    assert params["name"] == f"{APP_NAME}/reports/topIssues"
    assert params["pageSize"] == 25
    assert params["filter_interval_startTime"].endswith("Z")
    assert "filter_issue_errorTypes" not in params
    assert "pageToken" not in params


def test_list_issues_passes_filters() -> None:
    service = MagicMock()
    _reports(service).get.return_value.execute.return_value = {}

    _client_with(service).list_issues(
        "my-project",
        "1:123:android:abc",
        days=7,
        error_type="anr",
        state="open",
        search="BadParcelableException MainTabsScreen",
        page_size=500,
        page_token="tok",
    )

    params = _reports(service).get.call_args.kwargs
    assert params["filter_issue_errorTypes"] == ["ANR"]
    assert params["filter_issue_states"] == ["OPEN"]
    assert params["filter_issue_content"] == "BadParcelableException MainTabsScreen"
    assert params["pageToken"] == "tok"
    assert params["pageSize"] == 100  # clamped


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"error_type": "CRASH"}, "error_type must be one of"),
        ({"state": "RESOLVED"}, "state must be one of"),
    ],
)
def test_list_issues_rejects_unknown_enum_filters(
    kwargs: dict[str, str],
    message: str,
) -> None:
    service = MagicMock()

    with pytest.raises(PlayStoreClientError, match=message):
        _client_with(service).list_issues("my-project", "1:123:android:abc", **kwargs)

    _reports(service).get.assert_not_called()


def test_list_issues_wraps_http_error() -> None:
    service = MagicMock()
    _reports(service).get.return_value.execute.side_effect = _make_http_error("denied")

    with pytest.raises(
        PlayStoreClientError,
        match="Failed to list Firebase Crashlytics issues: denied",
    ):
        _client_with(service).list_issues("my-project", "1:123:android:abc")


def test_get_issue_uses_issue_resource_name() -> None:
    service = MagicMock()
    expected = {"name": ISSUE_NAME, "state": "OPEN"}
    _issues(service).get.return_value.execute.return_value = expected

    result = _client_with(service).get_issue("my-project", "1:123:android:abc", ISSUE_ID)

    assert result == expected
    _issues(service).get.assert_called_once_with(name=ISSUE_NAME)


def test_get_issue_rejects_malformed_issue_id() -> None:
    service = MagicMock()

    with pytest.raises(PlayStoreClientError, match="32-character lowercase hex"):
        _client_with(service).get_issue("my-project", "1:123:android:abc", "c07d6e04")

    _issues(service).get.assert_not_called()


def test_get_issue_wraps_http_error() -> None:
    service = MagicMock()
    _issues(service).get.return_value.execute.side_effect = _make_http_error("denied")

    with pytest.raises(
        PlayStoreClientError,
        match="Failed to get Firebase Crashlytics issue: denied",
    ):
        _client_with(service).get_issue("my-project", "1:123:android:abc", ISSUE_ID)


def test_close_issue_sets_closed_state() -> None:
    service = MagicMock()
    expected = {
        "name": ISSUE_NAME,
        "state": "CLOSED",
        "errorType": "ANR",
    }
    _issues(service).patch.return_value.execute.return_value = expected
    client = CrashlyticsClient(credentials_json={"type": "service_account"})
    client._service = service

    result = client.close_issue("my-project", "1:123:android:abc", ISSUE_ID)

    assert result == expected
    _issues(service).patch.assert_called_once_with(
        name=ISSUE_NAME,
        updateMask="state",
        body={
            "name": ISSUE_NAME,
            "state": "CLOSED",
        },
    )


def test_close_issue_wraps_http_error() -> None:
    service = MagicMock()
    _issues(service).patch.return_value.execute.side_effect = _make_http_error("denied")
    client = CrashlyticsClient(credentials_json={"type": "service_account"})
    client._service = service

    with pytest.raises(
        PlayStoreClientError,
        match="Failed to close Firebase Crashlytics issue: denied",
    ):
        client.close_issue("my-project", "1:123:android:abc", ISSUE_ID)


def test_close_issue_does_not_retry_server_errors() -> None:
    """An unknown issue ID yields 500 INTERNAL; retrying it only wastes the backoff."""
    service = MagicMock()
    error = _make_http_error("Internal error encountered.")
    error.resp.status = 500
    _issues(service).patch.return_value.execute.side_effect = error
    client = CrashlyticsClient(credentials_json={"type": "service_account"})
    client._service = service

    with pytest.raises(PlayStoreClientError):
        client.close_issue("my-project", "1:123:android:abc", ISSUE_ID)

    assert _issues(service).patch.return_value.execute.call_count == 1


def test_get_service_uses_firebase_scope_and_discovery_api() -> None:
    credentials = MagicMock()
    service = MagicMock()
    with (
        patch(
            "play_store_mcp.credentials.service_account.Credentials.from_service_account_info",
            return_value=credentials,
        ) as from_info,
        patch(
            "play_store_mcp.crashlytics_client.build",
            return_value=service,
        ) as build,
    ):
        client = CrashlyticsClient(credentials_json=json.dumps({"type": "service_account"}))

        assert client._get_service() is service

    from_info.assert_called_once_with(
        {"type": "service_account"},
        scopes=CRASHLYTICS_SCOPES,
    )
    # static_discovery must be False: firebasecrashlytics v1alpha has no bundled
    # discovery document, so the google-api-python-client 2.x default raises
    # UnknownApiNameOrVersion before any request is made.
    build.assert_called_once_with(
        "firebasecrashlytics",
        "v1alpha",
        credentials=credentials,
        cache_discovery=False,
        static_discovery=False,
    )


def test_client_uses_shared_credentials_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = {"type": "service_account", "project_id": "my-project"}
    monkeypatch.setenv("GOOGLE_PLAY_STORE_CREDENTIALS", json.dumps(credentials))

    client = CrashlyticsClient()

    assert client._credentials_json == json.dumps(credentials)


def test_close_crashlytics_issue_tool() -> None:
    client = MagicMock()
    client.close_issue.return_value = {"state": "CLOSED", "errorType": "FATAL"}

    with patch(
        "play_store_mcp.server.get_crashlytics_client_from_context",
        return_value=client,
    ):
        result = server.close_crashlytics_issue(
            "my-project",
            "1:123:android:abc",
            ISSUE_ID,
        )

    assert result == {"state": "CLOSED", "errorType": "FATAL"}
    client.close_issue.assert_called_once_with(
        project_id="my-project",
        app_id="1:123:android:abc",
        issue_id=ISSUE_ID,
    )


def test_list_crashlytics_issues_tool() -> None:
    client = MagicMock()
    client.list_issues.return_value = {"issues": [], "totalIssues": 0}

    with patch(
        "play_store_mcp.server.get_crashlytics_client_from_context",
        return_value=client,
    ):
        result = server.list_crashlytics_issues(
            "my-project",
            "1:123:android:abc",
            days=7,
            error_type="ANR",
            search="MainTabsScreen",
        )

    assert result == {"issues": [], "totalIssues": 0}
    client.list_issues.assert_called_once_with(
        project_id="my-project",
        app_id="1:123:android:abc",
        days=7,
        error_type="ANR",
        state="",
        search="MainTabsScreen",
        page_size=25,
        page_token="",
    )


def test_get_crashlytics_issue_tool() -> None:
    client = MagicMock()
    client.get_issue.return_value = {"name": ISSUE_NAME, "state": "OPEN"}

    with patch(
        "play_store_mcp.server.get_crashlytics_client_from_context",
        return_value=client,
    ):
        result = server.get_crashlytics_issue("my-project", "1:123:android:abc", ISSUE_ID)

    assert result == {"name": ISSUE_NAME, "state": "OPEN"}
    client.get_issue.assert_called_once_with(
        project_id="my-project",
        app_id="1:123:android:abc",
        issue_id=ISSUE_ID,
    )


def test_read_only_mode_allows_crashlytics_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listing and getting are GETs, so read-only mode must not block them."""
    monkeypatch.setattr(server, "READ_ONLY", True)
    client = MagicMock()
    monkeypatch.setattr(server, "get_crashlytics_client_from_context", lambda: client)

    server.list_crashlytics_issues("my-project", "1:123:android:abc")
    server.get_crashlytics_issue("my-project", "1:123:android:abc", ISSUE_ID)

    client.list_issues.assert_called_once()
    client.get_issue.assert_called_once()


def test_close_crashlytics_issue_respects_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "READ_ONLY", True)
    get_client = MagicMock()
    monkeypatch.setattr(server, "get_crashlytics_client_from_context", get_client)

    result = server.close_crashlytics_issue(
        "my-project",
        "1:123:android:abc",
        "issue-42",
    )

    assert "read-only mode" in result["error"]
    get_client.assert_not_called()
