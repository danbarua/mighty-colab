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

from unittest.mock import MagicMock

from colab_cli.client import ColabRequestError, XSSI_PREFIX, response_body_if_json


def _error(response_body, content_type):
    response = MagicMock()
    response.headers = {"Content-Type": content_type}
    return ColabRequestError(
        "failed", MagicMock(), response, response_body=response_body
    )


def test_response_body_if_json_returns_body_for_json_content_type():
    e = _error('{"code": 7, "message": "denied"}', "application/json; charset=utf-8")

    assert response_body_if_json(e) == '{"code": 7, "message": "denied"}'


def test_response_body_if_json_strips_xssi_prefix():
    e = _error(XSSI_PREFIX + '[7,"denied"]', "application/json")

    assert response_body_if_json(e) == '[7,"denied"]'


def test_response_body_if_json_returns_none_for_html_content_type():
    """The real, confirmed-live case: `assign`'s 400 rejection is Google's
    generic frontend HTML error page. Never worth writing that into a
    JSONL history file."""
    e = _error(
        "<html><body><b>400.</b> That’s an error.</body></html>",
        "text/html; charset=UTF-8",
    )

    assert response_body_if_json(e) is None


def test_response_body_if_json_returns_none_when_no_body():
    e = _error(None, "application/json")

    assert response_body_if_json(e) is None


def test_response_body_if_json_returns_none_when_no_response():
    """A non-HTTP failure (e.g. a network error) has no `.response`
    attribute at all -- must fail closed, not crash."""
    e = ConnectionError("network unreachable")

    assert response_body_if_json(e) is None


def test_response_body_if_json_returns_none_when_headers_missing():
    """Defensive: a malformed/mocked response without real headers must
    not raise -- fails closed to None."""
    response = MagicMock()
    del response.headers
    response.headers = None
    e = ColabRequestError(
        "failed", MagicMock(), response, response_body="some body"
    )

    assert response_body_if_json(e) is None


def test_response_body_if_json_truncates_to_limit():
    body = "x" * 2000
    e = _error(body, "application/json")

    assert response_body_if_json(e, limit=100) == "x" * 100
