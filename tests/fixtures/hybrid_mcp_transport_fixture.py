#!/usr/bin/env python3
"""Isolated MCP transport fixture for the hybrid Serena gateway tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_ready(port: int, process: subprocess.Popen[Any]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("fixture MCP server exited during startup")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("fixture MCP server did not become ready")


def _serve(role: str, port: int) -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        f"hybrid-{role}-fixture",
        host="127.0.0.1",
        port=port,
        log_level="ERROR",
        stateless_http=True,
        json_response=True,
    )
    if role == "primary":

        @server.tool(name="jet_brains_get_symbols_overview")
        def primary_overview(relative_path: str, depth: int = 0) -> str:
            del relative_path, depth
            return "primary"

    else:

        @server.tool(name="get_symbols_overview")
        def secondary_overview(relative_path: str, depth: int = 0) -> str:
            del relative_path, depth
            return "secondary"

    server.run(transport="streamable-http")


def _result_text(result: Any) -> str:
    return "".join(
        content.text
        for content in result.content
        if getattr(content, "type", None) == "text"
    )


async def _call_gateway(
    gateway: Path, root: Path, primary_port: int, secondary_port: int
) -> dict[str, object]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(gateway),
            "--project-root",
            str(root),
            "--primary-url",
            f"http://127.0.0.1:{primary_port}/mcp",
            "--secondary-url",
            f"http://127.0.0.1:{secondary_port}/mcp",
        ],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            native = await session.call_tool(
                "jet_brains_get_symbols_overview",
                {"relative_path": "src/example.c"},
                meta={"progressToken": "native-progress"},
            )
            typescript = await session.call_tool(
                "jet_brains_get_symbols_overview",
                {"relative_path": "src/example.ts"},
            )
    return {
        "native": _result_text(native),
        "typescript": _result_text(typescript),
        "tools": len(tools.tools),
    }


def _probe(gateway: Path) -> None:
    primary_port = _free_port()
    secondary_port = _free_port()
    processes = [
        subprocess.Popen(
            [sys.executable, __file__, "server", role, str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for role, port in (("primary", primary_port), ("secondary", secondary_port))
    ]
    try:
        for port, process in zip((primary_port, secondary_port), processes):
            _wait_ready(port, process)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "src").mkdir()
            (root / "src/example.c").write_text("int answer(void) { return 42; }\n")
            (root / "src/example.ts").write_text("export const answer = 42;\n")
            print(
                json.dumps(
                    asyncio.run(
                        _call_gateway(gateway, root, primary_port, secondary_port)
                    ),
                    sort_keys=True,
                )
            )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    server = subparsers.add_parser("server")
    server.add_argument("role", choices=("primary", "secondary"))
    server.add_argument("port", type=int)
    probe = subparsers.add_parser("probe")
    probe.add_argument("gateway", type=Path)
    args = parser.parse_args()
    if args.command == "server":
        _serve(args.role, args.port)
    else:
        _probe(args.gateway)


if __name__ == "__main__":
    main()
