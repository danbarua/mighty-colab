"""Remove credentials from error text before it is persisted or logged.

Stdlib only: this module is copied to the VM as part of mighty_runtime and is
also used by the local supervisor.

Signed data-plane URLs carry their signature in the query string, Contents
API requests carry the Colab runtime-proxy token there, and package index
URLs carry tokens as userinfo (`https://user:token@host/simple`). Exception
messages from urllib, http.client, requests, pip and uv can quote a request
target, absolute or relative, so every query string and every URL userinfo
in the text is replaced.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_QUERY = re.compile(r"\?(?=[^\s'\"<>,)]*=)[^\s'\"<>,)]*")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@'\"<>]+@")


def redact_credentials(text: str) -> str:
    """Replace each `?key=value...` query string with `?<redacted>` and each
    URL's `user:password@` with `***@`."""

    return _QUERY.sub("?<redacted>", _USERINFO.sub(r"\1***@", text))


def redact_url(text: str, url: str | None, identity: str) -> str:
    """`redact_credentials`, after replacing `url` with `identity` and its
    query string with `<redacted>` wherever they appear. An error body can
    echo a signed URL's query parameters without the leading `?` (a GCS
    SignatureDoesNotMatch body quotes the canonical request), which the
    general pattern does not match."""

    if url:
        text = text.replace(url, identity)
        try:
            query = urlsplit(url).query
        except ValueError:
            query = ""
        if query:
            text = text.replace(query, "<redacted>")
    return redact_credentials(text)


def describe_error(error: BaseException) -> str:
    """`Type: message` for a durable record, with credentials redacted."""

    return f"{type(error).__name__}: {redact_credentials(str(error))}"
