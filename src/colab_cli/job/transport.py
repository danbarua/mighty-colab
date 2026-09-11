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

"""Contents-API transport for long-running jobs.

The runtime proxy token returned by assignment is short lived.  A job must
therefore resolve its assignment again after an authorization-looking Contents
failure before it concludes that the VM or a file is gone.
"""

from __future__ import annotations

import base64
import json
import logging
import tempfile
import time
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any

import requests

import colab_cli.common as common
from colab_cli.contents import ContentsClient
from colab_cli.state import SessionState, StateStore
from colab_cli.utils import get_status_code


class ReadStatus(str, Enum):
    """Classification of a Contents read."""

    OK = "ok"
    NOT_FOUND = "not_found"
    DEGRADED = "degraded"
    SESSION_LOST = "session_lost"


class JobTransport:
    """Read job records through Contents with bounded failure handling.

    ``session_state`` is normally the session record persisted by the CLI.
    ``endpoint``/``token`` are accepted as a lightweight alternative for
    callers that already have those values.  ``client`` is the Colab API
    client, used only to resolve fresh runtime-proxy information; this class
    never calls ``execute_code``.
    """

    def __init__(
        self,
        session_state: SessionState | str | None = None,
        client: Any = None,
        store: StateStore | None = None,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        url: str | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 0.25,
        not_found_refresh_interval: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if endpoint is not None:
            if session_state is not None:
                raise TypeError("pass either session_state or endpoint, not both")
            session_state = endpoint

        if isinstance(session_state, SessionState):
            resolved_state = session_state
        elif isinstance(session_state, str):
            if token is None:
                raise TypeError("token is required when session_state is an endpoint")
            resolved_state = SessionState(
                name=session_state,
                endpoint=session_state,
                token=token,
                url=url or session_state,
            )
        elif session_state is None:
            if endpoint is None or token is None:
                raise TypeError("session_state or endpoint and token are required")
            resolved_state = SessionState(
                name=endpoint,
                endpoint=endpoint,
                token=token,
                url=url or endpoint,
            )
        else:
            raise TypeError("session_state must be a SessionState or endpoint")

        if client is None:
            raise TypeError("client is required")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("connect_timeout and read_timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_factor < 0:
            raise ValueError("backoff_factor must be non-negative")
        if not_found_refresh_interval < 0:
            raise ValueError("not_found_refresh_interval must be non-negative")

        self.session_state = resolved_state
        self.endpoint = resolved_state.endpoint
        self.client = client
        self.store = store if store is not None else common.state.store
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.not_found_refresh_interval = not_found_refresh_interval
        self._sleep = sleep
        self._logger = logging.getLogger(__name__)
        self._contents = ContentsClient(resolved_state)
        # None means assignment resolution failed; False means it completed
        # and proved that this endpoint is absent.
        self._last_endpoint_present: bool | None = None
        self._last_404_refresh_at: float | None = None

    @property
    def timeout(self) -> tuple[float, float]:
        """The explicit Requests connect/read timeout for every read."""

        return (self.connect_timeout, self.read_timeout)

    def read_json(self, remote_path: str) -> tuple[dict[str, Any] | None, ReadStatus]:
        """Read and decode a JSON file from the VM."""

        value, status = self._read(remote_path)
        if status is not ReadStatus.OK:
            return None, status
        if isinstance(value, dict):
            return value, status
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            self._logger.warning("Contents response for %s was not JSON", remote_path)
            return None, ReadStatus.DEGRADED
        if not isinstance(decoded, dict):
            self._logger.warning("JSON Contents response for %s was not an object", remote_path)
            return None, ReadStatus.DEGRADED
        return decoded, status

    def read_text(self, remote_path: str) -> tuple[str | None, ReadStatus]:
        """Read and decode a text file from the VM."""

        value, status = self._read(remote_path)
        if status is not ReadStatus.OK:
            return None, status
        if isinstance(value, str):
            return value, status
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8"), status
            except UnicodeDecodeError:
                return None, ReadStatus.DEGRADED
        return str(value), status

    def write_json(self, remote_path: str, value: Mapping[str, Any]) -> ReadStatus:
        """Write a JSON control record through Contents."""

        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ReadStatus.DEGRADED
        return self._write(remote_path, payload)

    def write_text(self, remote_path: str, value: str) -> ReadStatus:
        """Write a text control record through Contents."""

        return self._write(remote_path, value)

    def remove(self, remote_path: str) -> ReadStatus:
        """Delete a remote file and confirm it cannot still be read."""
        refresh_attempted = False
        transient_retries = 0

        while True:
            try:
                self._contents._request("DELETE", remote_path, timeout=self.timeout)
                break
            except FileNotFoundError:
                break
            except Exception as error:  # Contents and Requests expose several exception types.
                status_code = self._status_code(error)
                if status_code in (401, 404) and not refresh_attempted:
                    refresh_attempted = True
                    refresh_status = self._refresh_token()
                    if refresh_status is not ReadStatus.OK:
                        return refresh_status
                    continue
                if self._is_transient(error) and transient_retries < self.max_retries:
                    self._sleep(self.backoff_factor * (2**transient_retries))
                    transient_retries += 1
                    continue
                return self._classify_endpoint()

        confirmation_refresh_attempted = False
        transient_retries = 0
        while True:
            try:
                self._contents._request(
                    "GET",
                    remote_path,
                    params={"content": "0"},
                    timeout=self.timeout,
                )
                return ReadStatus.DEGRADED
            except FileNotFoundError:
                return ReadStatus.OK
            except Exception as error:  # Contents and Requests expose several exception types.
                status_code = self._status_code(error)
                if status_code in (401, 404) and not confirmation_refresh_attempted:
                    confirmation_refresh_attempted = True
                    refresh_status = self._refresh_token()
                    if refresh_status is ReadStatus.SESSION_LOST:
                        return ReadStatus.OK
                    if refresh_status is not ReadStatus.OK:
                        return ReadStatus.DEGRADED
                    continue
                if self._is_transient(error) and transient_retries < self.max_retries:
                    self._sleep(self.backoff_factor * (2**transient_retries))
                    transient_retries += 1
                    continue
                return ReadStatus.DEGRADED

    def _write(self, remote_path: str, payload: str) -> ReadStatus:
        refresh_attempted = False
        transient_retries = 0

        while True:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".job", delete=True
                ) as local_file:
                    local_file.write(payload)
                    local_file.flush()
                    self._contents.upload(
                        local_file.name,
                        remote_path,
                        timeout=self.timeout,
                    )
                return ReadStatus.OK
            except Exception as error:  # Contents and Requests expose several exception types.
                status_code = self._status_code(error)

                if status_code in (401, 404) and not refresh_attempted:
                    refresh_attempted = True
                    if status_code == 404:
                        self._last_404_refresh_at = time.monotonic()
                    refresh_status = self._refresh_token()
                    if refresh_status is ReadStatus.SESSION_LOST:
                        return refresh_status
                    if refresh_status is not ReadStatus.OK:
                        return ReadStatus.DEGRADED
                    continue

                if self._is_transient(error) and transient_retries < self.max_retries:
                    self._sleep(self.backoff_factor * (2**transient_retries))
                    transient_retries += 1
                    continue

                if status_code == 404 and refresh_attempted:
                    return ReadStatus.NOT_FOUND
                if status_code in (401, 404):
                    return self._classify_endpoint()
                return self._classify_endpoint()

    def _read(self, remote_path: str) -> tuple[Any | None, ReadStatus]:
        refresh_attempted = False
        transient_retries = 0

        while True:
            try:
                payload = self._request(remote_path)
                return self._decode_contents_payload(payload)
            except Exception as error:  # Requests and Contents use several exception types.
                status_code = self._status_code(error)

                if status_code in (401, 404) and not refresh_attempted:
                    if status_code == 404 and not self._should_refresh_404():
                        return None, ReadStatus.NOT_FOUND
                    refresh_attempted = True
                    refresh_status = self._refresh_token()
                    if refresh_status is ReadStatus.SESSION_LOST:
                        return None, refresh_status
                    if refresh_status is not ReadStatus.OK:
                        return None, ReadStatus.DEGRADED
                    if status_code == 404:
                        self._last_404_refresh_at = time.monotonic()
                    # The next request is the one and only request after token
                    # refresh.  A second 401/404 is classified below without
                    # another assignment lookup.
                    continue

                if self._is_transient(error) and transient_retries < self.max_retries:
                    self._sleep(self.backoff_factor * (2**transient_retries))
                    transient_retries += 1
                    continue

                if status_code == 404 and refresh_attempted:
                    # A successful assignment refresh followed by a 404 means
                    # the endpoint is healthy and the path itself is absent.
                    return None, ReadStatus.NOT_FOUND
                if status_code in (401, 404):
                    return None, self._classify_endpoint()
                return None, self._classify_endpoint()

    def _should_refresh_404(self) -> bool:
        if self._last_404_refresh_at is None:
            return True
        return (
            time.monotonic() - self._last_404_refresh_at
            >= self.not_found_refresh_interval
        )

    def _request(self, remote_path: str) -> Any:
        """Issue one Contents GET with explicit connect/read timeouts."""

        return self._contents._request(
            "GET",
            remote_path,
            params={"content": "1"},
            timeout=self.timeout,
        )

    def _refresh_token(self) -> ReadStatus:
        """Resolve and persist one fresh runtime-proxy token."""

        assignment, endpoint_present = self._find_assignment()
        self._last_endpoint_present = endpoint_present
        if endpoint_present is False:
            return ReadStatus.SESSION_LOST
        if endpoint_present is None:
            return ReadStatus.DEGRADED
        if assignment is None:
            return ReadStatus.DEGRADED

        token, proxy_url = self._proxy_values(assignment)
        if not token:
            return ReadStatus.DEGRADED

        updates: dict[str, str] = {"token": token}
        if proxy_url:
            updates["url"] = proxy_url
        self.session_state = self.session_state.model_copy(update=updates)
        self._contents = ContentsClient(self.session_state)

        # Keep the durable session record current.  In particular, use the
        # original name rather than endpoint so adopt/status see this token.
        try:
            self.store.add(self.session_state)
        except Exception:  # Persistence must not hide the read classification.
            self._logger.warning(
                "Could not persist refreshed token for session %s",
                self.session_state.name,
                exc_info=True,
            )
        return ReadStatus.OK

    def _find_assignment(self) -> tuple[Any | None, bool | None]:
        try:
            assignments = self.client.list_assignments()
            for assignment in assignments:
                if self._assignment_endpoint(assignment) == self.endpoint:
                    return assignment, True
        except Exception:
            return None, None
        return None, False

    def _classify_endpoint(self) -> ReadStatus:
        if self._last_endpoint_present is True:
            return ReadStatus.DEGRADED
        if self._last_endpoint_present is False:
            return ReadStatus.SESSION_LOST
        # A transport failure while resolving assignments cannot prove that
        # the endpoint disappeared, so it is degraded rather than lost.
        assignment, present = self._find_assignment()
        del assignment
        self._last_endpoint_present = present
        return ReadStatus.SESSION_LOST if present is False else ReadStatus.DEGRADED

    @staticmethod
    def _assignment_endpoint(assignment: Any) -> str | None:
        if isinstance(assignment, Mapping):
            return assignment.get("endpoint")
        return getattr(assignment, "endpoint", None)

    @staticmethod
    def _proxy_values(assignment: Any) -> tuple[str | None, str | None]:
        if isinstance(assignment, Mapping):
            proxy = assignment.get("runtime_proxy_info")
            if proxy is None:
                proxy = assignment.get("runtimeProxyInfo")
        else:
            proxy = getattr(assignment, "runtime_proxy_info", None)
        if isinstance(proxy, Mapping):
            return proxy.get("token"), proxy.get("url")
        if proxy is None:
            return None, None
        return getattr(proxy, "token", None), getattr(proxy, "url", None)

    @staticmethod
    def _status_code(error: Exception) -> int | None:
        if isinstance(error, FileNotFoundError):
            return 404
        return get_status_code(error)

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        status_code = JobTransport._status_code(error)
        return bool(status_code is not None and 500 <= status_code < 600) or isinstance(
            error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        )

    @staticmethod
    def _decode_contents_payload(payload: Any) -> tuple[Any | None, ReadStatus]:
        if not isinstance(payload, Mapping):
            return payload, ReadStatus.OK
        if "content" not in payload:
            return dict(payload), ReadStatus.OK

        content = payload["content"]
        if payload.get("format") != "base64":
            return content, ReadStatus.OK
        try:
            return base64.b64decode(content).decode("utf-8"), ReadStatus.OK
        except (ValueError, TypeError, UnicodeDecodeError):
            return None, ReadStatus.DEGRADED
