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
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import os
from unittest.mock import MagicMock, patch

import pytest
from colab_cli.contents import CHUNK_SIZE, ContentsClient
from requests import Response

from colab_cli.state import SessionState


@pytest.fixture
def session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://fake-endpoint.colab.dev",
        endpoint="endpoint",
    )


@pytest.fixture
def client(session):
    return ContentsClient(session)


@patch("colab_cli.contents.requests.request")
def test_list_dir(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "content",
        "type": "directory",
        "content": [
            {"name": "file.txt", "type": "file"},
            {"name": "dir", "type": "directory"},
        ],
    }
    mock_request.return_value = mock_resp

    res = client.list_dir("content")

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json=None,
    )
    assert res["type"] == "directory"
    assert len(res["content"]) == 2


@patch("colab_cli.contents.requests.request")
def test_rm_file(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 204
    mock_request.return_value = mock_resp

    client.rm("content/file.txt")

    mock_request.assert_called_once_with(
        "DELETE",
        "https://fake-endpoint.colab.dev/api/contents/content/file.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json=None,
    )


@patch("colab_cli.contents.requests.request")
def test_404_error(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 404
    mock_request.return_value = mock_resp

    with pytest.raises(FileNotFoundError):
        client.list_dir("nonexistent")


@patch("colab_cli.contents.requests.request")
def test_upload_500_error_hints_at_size_limit(mock_request, client, tmp_path):
    """A bare 500 from the Colab/Jupyter backend on upload is a known-but-
    undocumented failure mode (likely a size limit with no published
    threshold). We can't enforce a limit we don't know, but the error
    message should at least point at the likely cause instead of a bare
    'Internal Server Error'."""
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 500
    mock_request.return_value = mock_resp

    local_file = tmp_path / "big.bin"
    local_file.write_bytes(b"x")

    with pytest.raises(Exception, match="undocumented"):
        client.upload(str(local_file), "content/big.bin")


@patch("colab_cli.contents.requests.request")
def test_upload_small_file_is_still_a_single_request(mock_request, client, tmp_path):
    """Regression guard: files at/under CHUNK_SIZE must keep today's
    single-request, chunk=1 behavior exactly -- no change for the common
    case."""
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    local_file = tmp_path / "small.bin"
    local_file.write_bytes(b"x" * 100)

    client.upload(str(local_file), "content/small.bin")

    assert mock_request.call_count == 1
    _, kwargs = mock_request.call_args
    assert kwargs["json"]["chunk"] == 1


@patch("colab_cli.contents.requests.request")
def test_upload_large_file_is_chunked_with_correct_sentinel(mock_request, client, tmp_path):
    """Files over CHUNK_SIZE must be sliced into CHUNK_SIZE pieces sent as
    sequential requests numbered 1, 2, ..., with the final request flagged
    chunk=-1 -- the exact protocol JupyterLab's own client uses. Also
    verifies reassembling each chunk's content reproduces the original bytes
    exactly, to catch an off-by-one in the slicing loop."""
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    original = os.urandom(CHUNK_SIZE * 2 + 100)
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(original)

    client.upload(str(local_file), "content/big.bin")

    assert mock_request.call_count == 3
    chunks = [call.kwargs["json"]["chunk"] for call in mock_request.call_args_list]
    assert chunks == [1, 2, -1]

    reassembled = b"".join(
        base64.b64decode(call.kwargs["json"]["content"])
        for call in mock_request.call_args_list
    )
    assert reassembled == original


@patch("colab_cli.contents.requests.request")
def test_upload_file_size_exact_multiple_of_chunk_size(mock_request, client, tmp_path):
    """Boundary case most likely to have an off-by-one: a file whose size is
    an exact multiple of CHUNK_SIZE must still end with exactly one chunk=-1
    request, not an extra trailing empty-content request."""
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    original = os.urandom(CHUNK_SIZE * 2)
    local_file = tmp_path / "exact.bin"
    local_file.write_bytes(original)

    client.upload(str(local_file), "content/exact.bin")

    assert mock_request.call_count == 2
    chunks = [call.kwargs["json"]["chunk"] for call in mock_request.call_args_list]
    assert chunks == [1, -1]


@patch("colab_cli.contents.requests.request")
def test_download_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200

    # Mocking a base64 encoded response
    content_bytes = b"Hello world!"
    b64_content = base64.b64encode(content_bytes).decode("ascii")

    mock_resp.json.return_value = {
        "name": "test.txt",
        "type": "file",
        "format": "base64",
        "content": b64_content,
    }
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    client.download("content/test.txt", str(local_file))

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={
            "authuser": "0",
            "colab-runtime-proxy-token": "test-token",
            "content": "1",
        },
        json=None,
    )

    assert local_file.read_bytes() == content_bytes


@patch("colab_cli.contents.requests.request")
def test_upload_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    content_bytes = b"Hello upload!"
    local_file.write_bytes(content_bytes)

    client.upload(str(local_file), "content/test.txt")

    expected_b64 = base64.b64encode(content_bytes).decode("ascii")

    mock_request.assert_called_once_with(
        "PUT",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json={
            "name": "test.txt",
            "path": "content/test.txt",
            "type": "file",
            "format": "base64",
            "content": expected_b64,
            "chunk": 1,
        },
    )
