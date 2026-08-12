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

"""`setup_logging`: default level is INFO, DEBUG is opt-in via `--debug`.

Third-party libraries (urllib3, jupyter_kernel_client, websocket) set no
level of their own, so they inherit whatever the root logger is set to --
a DEBUG-by-default root meant every invocation's log (and stderr, under
--logtostderr/--json) filled with their internal chatter."""

import logging

import pytest

from colab_cli.common import setup_logging


@pytest.fixture
def _isolated_root_logger():
    """`setup_logging` mutates the real root/urllib3 loggers (level +
    handlers) with no reset mechanism of its own -- save and restore both
    so this test's DEBUG/INFO flips can't leak into other tests."""
    root = logging.getLogger()
    urllib3_logger = logging.getLogger("urllib3")
    saved = (root.level, list(root.handlers), urllib3_logger.level)
    yield
    root.level, root.handlers, urllib3_logger.level = saved


def test_setup_logging_defaults_to_info(mocker, _isolated_root_logger):
    mocker.patch("colab_cli.common.RotatingFileHandler")
    setup_logging(log_to_stderr=False)
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("urllib3").level == logging.INFO


def test_setup_logging_debug_true_enables_debug(mocker, _isolated_root_logger):
    mocker.patch("colab_cli.common.RotatingFileHandler")
    setup_logging(log_to_stderr=False, debug=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.DEBUG


def test_setup_logging_caps_log_file_size(mocker, _isolated_root_logger):
    """Every CLI invocation is a fresh process that re-runs setup_logging
    and re-opens the same file -- a plain FileHandler has no cap, so it
    grows forever across the tool's lifetime (observed: colab.log at
    192MB from about a week of normal use). Must use a RotatingFileHandler
    with an actual maxBytes/backupCount, not just any handler."""
    mock_handler_cls = mocker.patch("colab_cli.common.RotatingFileHandler")
    setup_logging(log_to_stderr=False)

    mock_handler_cls.assert_called_once()
    kwargs = mock_handler_cls.call_args.kwargs
    assert kwargs["maxBytes"] > 0
    assert kwargs["backupCount"] > 0
