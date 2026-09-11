# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from colab_cli.job.transport import JobTransport, ReadStatus
from colab_cli.state import SessionState


@pytest.fixture
def session_state():
    return SessionState(
        name="job-session",
        token="expired-token",
        url="https://proxy.example",
        endpoint="endpoint-1",
    )


@pytest.fixture
def assignment():
    return SimpleNamespace(
        endpoint="endpoint-1",
        runtime_proxy_info=SimpleNamespace(
            token="fresh-token",
            url="https://fresh-proxy.example",
        ),
    )


def http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(response=response)


def make_transport(mocker, session_state, contents, client, store):
    contents_factory = mocker.patch("colab_cli.job.transport.ContentsClient")
    refreshed_contents = MagicMock()
    contents_factory.side_effect = [contents, refreshed_contents]
    transport = JobTransport(
        session_state,
        client,
        store,
        connect_timeout=2,
        read_timeout=7,
        backoff_factor=0,
        sleep=MagicMock(),
    )
    return transport, contents_factory, refreshed_contents


def test_401_refreshes_token_persists_state_and_retries(
    mocker, session_state, assignment
):
    contents = MagicMock()
    contents._request.side_effect = [http_error(401)]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, contents_factory, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents._request.return_value = {
        "format": "text",
        "content": '{"workload":"running"}',
    }

    result, status = transport.read_json("content/result.json")

    assert result == {"workload": "running"}
    assert status is ReadStatus.OK
    client.list_assignments.assert_called_once_with()
    store.add.assert_called_once()
    persisted = store.add.call_args.args[0]
    assert persisted.name == "job-session"
    assert persisted.token == "fresh-token"
    assert persisted.url == "https://fresh-proxy.example"
    assert contents_factory.call_count == 2
    assert contents._request.call_args.kwargs["timeout"] == (2, 7)
    assert refreshed_contents._request.call_args.kwargs["timeout"] == (2, 7)


def test_persistent_401_is_degraded_not_session_lost(mocker, session_state, assignment):
    contents = MagicMock()
    contents._request.side_effect = [http_error(401)]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, contents_factory, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents._request.side_effect = [http_error(401)]

    result, status = transport.read_text("content/result.txt")

    assert result is None
    assert status is ReadStatus.DEGRADED
    client.list_assignments.assert_called_once_with()
    assert contents_factory.call_count == 2


def test_absent_endpoint_is_session_lost(mocker, session_state):
    contents = MagicMock()
    contents._request.side_effect = [http_error(401)]
    client = MagicMock()
    client.list_assignments.return_value = []
    store = MagicMock()
    transport, contents_factory, _ = make_transport(
        mocker, session_state, contents, client, store
    )

    result, status = transport.read_text("content/result.txt")

    assert result is None
    assert status is ReadStatus.SESSION_LOST
    client.list_assignments.assert_called_once_with()
    contents_factory.assert_called_once_with(session_state)
    store.add.assert_not_called()


def test_refresh_is_attempted_exactly_once_per_read(mocker, session_state, assignment):
    contents = MagicMock()
    contents._request.side_effect = [http_error(401)]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, contents_factory, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents._request.side_effect = [http_error(404)]

    result, status = transport.read_text("content/result.txt")

    assert result is None
    assert status is ReadStatus.NOT_FOUND
    client.list_assignments.assert_called_once_with()
    assert contents_factory.call_count == 2


def test_missing_path_on_healthy_endpoint_is_not_found(
    mocker, session_state, assignment
):
    contents = MagicMock()
    contents._request.side_effect = [FileNotFoundError("missing")]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, contents_factory, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents._request.side_effect = [FileNotFoundError("missing")]

    result, status = transport.read_text("content/missing.txt")

    assert result is None
    assert status is ReadStatus.NOT_FOUND
    client.list_assignments.assert_called_once_with()
    assert contents_factory.call_count == 2


def test_401_write_refreshes_and_retries(mocker, session_state, assignment):
    contents = MagicMock()
    contents.upload.side_effect = [http_error(401)]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, contents_factory, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents.upload.return_value = {}

    status = transport.write_json("content/cancel.json", {"cancel": True})

    assert status is ReadStatus.OK
    client.list_assignments.assert_called_once_with()
    assert contents_factory.call_count == 2
    assert contents.upload.call_args.kwargs["timeout"] == (2, 7)
    assert refreshed_contents.upload.call_args.kwargs["timeout"] == (2, 7)
    assert store.add.call_args.args[0].token == "fresh-token"


def test_repeated_missing_poll_does_not_refresh_every_time(
    mocker, session_state, assignment
):
    contents = MagicMock()
    contents._request.side_effect = [FileNotFoundError("missing")]
    client = MagicMock()
    client.list_assignments.return_value = [assignment]
    store = MagicMock()
    transport, _, refreshed_contents = make_transport(
        mocker, session_state, contents, client, store
    )
    refreshed_contents._request.side_effect = [
        FileNotFoundError("missing"),
        FileNotFoundError("missing"),
    ]

    first = transport.read_text("content/result.json")
    second = transport.read_text("content/result.json")

    assert first == (None, ReadStatus.NOT_FOUND)
    assert second == (None, ReadStatus.NOT_FOUND)
    client.list_assignments.assert_called_once_with()


def test_remove_deletes_and_confirms_the_remote_file_is_absent(
    mocker, session_state
):
    contents = MagicMock()
    contents._request.side_effect = [None, FileNotFoundError("gone")]
    client = MagicMock()
    store = MagicMock()
    transport, _factory, _refreshed = make_transport(
        mocker, session_state, contents, client, store
    )
    transport._last_404_refresh_at = float("inf")

    status = transport.remove("content/jobs/x/mighty_runtime/.secrets/transfer.json")

    assert status is ReadStatus.OK
    assert contents._request.call_args_list[0].args == (
        "DELETE",
        "content/jobs/x/mighty_runtime/.secrets/transfer.json",
    )
    assert contents._request.call_args_list[0].kwargs["timeout"] == (2, 7)
    assert contents._request.call_args_list[1].args == (
        "GET",
        "content/jobs/x/mighty_runtime/.secrets/transfer.json",
    )
    assert contents._request.call_args_list[1].kwargs["params"] == {"content": "0"}