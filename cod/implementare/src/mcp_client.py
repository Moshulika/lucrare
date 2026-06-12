"""
vibe-cli — MCP client manager.

Wraps `langchain_mcp_adapters.client.MultiServerMCPClient`. Reads server
definitions from preferences (`mcp_servers`), opens connections, and
exposes:

  - tools (list[BaseTool], names prefixed with the server name)
  - resources via a synthetic `mcp_read_resource` LangChain tool
  - prompts via a `get_prompt(server, name, args)` async method

The graph imports `mcp_manager` and merges its tools into the active
tool list. Failures on a single server do not block others.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool, tool

from src.logging_setup import emit_event, get_logger
from ui import preferences

if TYPE_CHECKING:
    # Deferred — langchain_mcp_adapters pulls in the full `mcp` protocol
    # package (~200ms cold), and it's only needed inside start() when
    # actually connecting to servers. Importing at the use site keeps
    # cold startup fast for users without any MCP servers configured.
    from langchain_mcp_adapters.client import MultiServerMCPClient

_log = get_logger("mcp")

DEFAULT_MAX_ACTIVE = 10


def _get_max_active() -> int:
    """Read `mcp.max_active` from the loaded config, with a safe fallback.

    Imported lazily so the mcp_client module stays importable even when the
    main config hasn't finished loading (e.g. in unit tests).
    """
    try:
        from src.main import config as _cfg

        return int((_cfg.get("mcp") or {}).get("max_active") or DEFAULT_MAX_ACTIVE)
    except Exception:
        return DEFAULT_MAX_ACTIVE


@dataclass
class ServerStatus:
    name: str
    enabled: bool
    connected: bool = False
    error: str | None = None
    tool_count: int = 0
    resource_count: int = 0
    prompt_count: int = 0


@dataclass
class MCPState:
    client: MultiServerMCPClient | None = None
    statuses: dict[str, ServerStatus] = field(default_factory=dict)
    tools_by_server: dict[str, list[BaseTool]] = field(default_factory=dict)
    resources: list[Any] = field(default_factory=list)
    prompts_by_server: dict[str, list[str]] = field(default_factory=dict)


_state = MCPState()


def _build_connections() -> dict[str, dict]:
    """Return a {server_name: connection_dict} for enabled servers."""
    out: dict[str, dict] = {}
    for name, cfg in preferences.get_mcp_servers().items():
        if not cfg.get("enabled", True):
            continue
        transport = cfg.get("transport", "stdio")
        if transport == "stdio":
            conn = {
                "transport": "stdio",
                "command": cfg["command"],
                "args": cfg.get("args", []),
            }
            if cfg.get("env"):
                conn["env"] = cfg["env"]
            if cfg.get("cwd"):
                conn["cwd"] = cfg["cwd"]
        elif transport in ("sse", "streamable_http"):
            conn = {
                "transport": transport,
                "url": cfg["url"],
            }
            if cfg.get("headers"):
                conn["headers"] = cfg["headers"]
        else:
            continue
        out[name] = conn
    return out


def _prefix_tool(server_name: str, t: BaseTool) -> BaseTool:
    """Prefix a tool's name with the server it came from to avoid collisions."""
    if not t.name.startswith(f"{server_name}__"):
        # BaseTool is a pydantic model — use model_copy to get a mutable copy.
        try:
            return t.model_copy(update={"name": f"{server_name}__{t.name}"})
        except Exception:
            try:
                t.name = f"{server_name}__{t.name}"
            except Exception:
                pass
    return t


def get_status() -> list[ServerStatus]:
    return list(_state.statuses.values())


def get_tools() -> list[BaseTool]:
    out: list[BaseTool] = []
    for tools in _state.tools_by_server.values():
        out.extend(tools)
    out.append(_resource_tool())
    return out


def get_resources() -> list[Any]:
    return list(_state.resources)


def get_prompts_by_server() -> dict[str, list[str]]:
    return dict(_state.prompts_by_server)


@tool
async def mcp_read_resource(uri: str) -> str:
    """Read an MCP resource by URI (returned in the /resources list).

    Args:
        uri: The resource URI to fetch.
    """
    if _state.client is None:
        return "MCP not initialized."
    try:
        blobs = await _state.client.get_resources(uris=uri)
    except Exception as e:
        return f"Failed to read resource {uri}: {e}"
    if not blobs:
        return f"No resource found at {uri}"
    parts: list[str] = []
    for b in blobs:
        data = getattr(b, "data", None)
        if isinstance(data, bytes):
            try:
                parts.append(data.decode("utf-8", errors="replace"))
            except Exception:
                parts.append(repr(data))
        elif data is not None:
            parts.append(str(data))
    return "\n".join(parts) if parts else "(empty resource)"


def _resource_tool() -> BaseTool:
    return mcp_read_resource


async def _refresh_server(name: str):
    """Pull tools/resources/prompts for one server. Catches and records errors."""
    assert _state.client is not None
    status = _state.statuses[name]
    t0 = time.perf_counter()
    try:
        raw_tools = await _state.client.get_tools(server_name=name)
        prefixed = [_prefix_tool(name, t) for t in raw_tools]
        _state.tools_by_server[name] = prefixed
        status.tool_count = len(prefixed)

        try:
            resources = await _state.client.get_resources(name)
            status.resource_count = len(resources)
        except Exception:
            resources = []
            status.resource_count = 0
        # Resources tagged by server for /resources display.
        _state.resources = [
            r for srv, rs in [(name, resources)] for r in rs
        ] + [r for r in _state.resources if getattr(r, "_server", None) != name]
        for r in resources:
            try:
                setattr(r, "_server", name)
            except Exception:
                pass

        try:
            async with _state.client.session(name) as session:
                listed = await session.list_prompts()
                _state.prompts_by_server[name] = [p.name for p in listed.prompts]
                status.prompt_count = len(listed.prompts)
        except Exception:
            _state.prompts_by_server[name] = []
            status.prompt_count = 0

        status.connected = True
        status.error = None
        emit_event(
            _log,
            "mcp.server_connected",
            server=name,
            tool_count=status.tool_count,
            resource_count=status.resource_count,
            prompt_count=status.prompt_count,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )
    except Exception as e:
        status.connected = False
        status.error = str(e)
        _state.tools_by_server[name] = []
        status.tool_count = 0
        emit_event(
            _log,
            "mcp.server_failed",
            server=name,
            error_type=type(e).__name__,
            error_msg=str(e)[:200],
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )


async def start(timeout_per_server: float = 8.0) -> list[ServerStatus]:
    """Connect to all enabled MCP servers. Idempotent — safe to call again
    after preferences change."""
    await stop()  # tear down any previous client

    connections = _build_connections()
    max_active = _get_max_active()
    # Cap enabled servers at `mcp.max_active`. Iteration follows preferences
    # dict order; surplus enabled servers stay visible but get a "skipped"
    # error so they show up as failed in /mcp instead of silently dropped.
    if len(connections) > max_active:
        kept = dict(list(connections.items())[:max_active])
        skipped = set(connections) - set(kept)
        connections = kept
    else:
        skipped = set()
    emit_event(
        _log,
        "mcp.start",
        server_count=len(connections),
        skipped_count=len(skipped),
        max_active=max_active,
    )
    # Track every configured server (including disabled) for the UI.
    _state.statuses = {
        name: ServerStatus(name=name, enabled=cfg.get("enabled", True))
        for name, cfg in preferences.get_mcp_servers().items()
    }
    for name in skipped:
        st = _state.statuses.get(name)
        if st is not None:
            st.connected = False
            st.error = f"skipped — over mcp.max_active cap of {max_active}"
    if not connections:
        return list(_state.statuses.values())

    from langchain_mcp_adapters.client import MultiServerMCPClient

    _state.client = MultiServerMCPClient(connections=connections)

    async def _refresh_with_timeout(name: str):
        try:
            await asyncio.wait_for(_refresh_server(name), timeout=timeout_per_server)
        except asyncio.TimeoutError:
            st = _state.statuses[name]
            st.connected = False
            st.error = f"timeout after {timeout_per_server:.0f}s"
            _state.tools_by_server[name] = []

    await asyncio.gather(
        *[_refresh_with_timeout(n) for n in connections],
        return_exceptions=True,
    )
    return list(_state.statuses.values())


async def stop():
    """Tear down the client. The langchain-mcp-adapters client manages
    connection lifecycle internally via session() so we just drop the ref."""
    had_client = _state.client is not None
    _state.client = None
    _state.tools_by_server.clear()
    _state.resources.clear()
    _state.prompts_by_server.clear()
    if had_client:
        emit_event(_log, "mcp.stop")


async def reload(server_name: str | None = None) -> list[ServerStatus]:
    """Reconnect a single server (or all if None)."""
    emit_event(_log, "mcp.reload", server=server_name)
    if server_name is None:
        return await start()
    if _state.client is None:
        return await start()
    if server_name not in _build_connections():
        return list(_state.statuses.values())
    await _refresh_server(server_name)
    return list(_state.statuses.values())


async def fetch_prompt(
    server_name: str, prompt_name: str, arguments: dict[str, Any] | None = None
) -> str:
    """Fetch an MCP prompt and render it as a single string."""
    if _state.client is None:
        return ""
    msgs = await _state.client.get_prompt(
        server_name, prompt_name, arguments=arguments
    )
    parts: list[str] = []
    for m in msgs:
        c = getattr(m, "content", "")
        if isinstance(c, list):
            parts.extend(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in c
            )
        else:
            parts.append(str(c))
    return "\n".join(p for p in parts if p)