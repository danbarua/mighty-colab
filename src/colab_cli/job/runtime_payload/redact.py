"""Remove credentials from error text before it is persisted or logged.

Stdlib only: this module is copied to the VM as part of mighty_runtime and is
also used by the local supervisor.

Signed data-plane URLs carry their signature in the query string, and Contents
API requests carry the Colab runtime-proxy token there. Exception messages from
urllib, http.client and requests can quote a request target, absolute or
relative, so every query string in the text is replaced.
"""

from __future__ import annotations

import re

_QUERY = re.compile(r"\?(?=[^\s'\"<>,)]*=)[^\s'\"<>,)]*")


def redact_queries(text: str) -> str:
    """Replace each `?key=value...` query string in `text` with `?<redacted>`."""

    return _QUERY.sub("?<redacted>", text)


def describe_error(error: BaseException) -> str:
    """`Type: message` for a durable record, with query strings redacted."""

    return f"{type(error).__name__}: {redact_queries(str(error))}"
