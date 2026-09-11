# Copyright (c) 2023-2024 Datalayer, Inc.
#
# BSD 3-Clause License

"""Jupyter Kernel Client through websocket.

VENDORED -- see VENDOR.md in this directory. Imports are relative so this
package resolves to itself and can never silently bind to a same-named
distribution installed in the environment.

`KonsoleApp`/`shell` are intentionally not vendored: they implement an
interactive console app this CLI never launches, and they pull
`jupyter-console`.
"""

from ._version import __version__
from .client import KernelClient
from .manager import KernelHttpManager
from .models import VariableDescription
from .snippets import SNIPPETS_REGISTRY, LanguageSnippets
from .wsclient import JupyterSubprotocol, KernelWebSocketClient

__all__ = [
    "SNIPPETS_REGISTRY",
    "KernelClient",
    "KernelHttpManager",
    "KernelWebSocketClient",
    "LanguageSnippets",
    "VariableDescription",
    "JupyterSubprotocol",
    "__version__",
]
