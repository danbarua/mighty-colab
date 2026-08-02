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

import asyncio

import typer


def mcp_command():
    """Start an MCP server exposing this CLI's commands as tools"""
    import typer.main

    from colab_cli.cli import app
    from colab_cli.mcp_server import run_stdio_server

    click_group = typer.main.get_command(app)
    asyncio.run(run_stdio_server(click_group, server_name="mighty-colab"))


def register(app: typer.Typer):
    app.command(name="mcp")(mcp_command)
