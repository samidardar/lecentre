"""Connexion aux serveurs MCP (CRM, calendrier, outils internes…) exposés comme outils aux agents.

Config : MCP_SERVERS_JSON='{"crm": {"command": "python", "args": ["mcp/crm/server.py"]}, "cal": {"url": "http://…/mcp"}}'
Aucune dépendance dure : si le SDK `mcp` est absent ou un serveur tombe, les agents continuent sans ces outils.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class McpTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", f"mcp_{self.server}_{self.name}")[:64]


class McpManager:
    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self.tools: dict[str, McpTool] = {}

    async def start(self, servers: dict[str, dict[str, Any]] | None = None) -> None:
        servers = servers if servers is not None else json.loads(get_settings().mcp_servers_json or "{}")
        if not servers:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client import streamable_http
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.warning("SDK mcp non installé (pip install callwiz[mcp]) : serveurs MCP ignorés")
            return
        for name, spec in servers.items():
            try:
                if "url" in spec:
                    if hasattr(streamable_http, "streamable_http_client"):  # SDK mcp ≥ 2
                        streams = await self._stack.enter_async_context(streamable_http.streamable_http_client(spec["url"]))
                    else:  # SDK mcp 1.x
                        streams = await self._stack.enter_async_context(
                            streamable_http.streamablehttp_client(spec["url"], headers=spec.get("headers")))
                    read, write = streams[0], streams[1]
                else:
                    params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env"))
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), 15)
                listed = await session.list_tools()
                self._sessions[name] = session
                for t in listed.tools:
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
                    tool = McpTool(name, t.name, t.description or t.name, dict(schema))
                    self.tools[tool.qualified_name] = tool
                logger.info("serveur MCP connecté", extra={"extra_fields": {"server": name, "tools": len(listed.tools)}})
            except Exception as exc:
                logger.warning("serveur MCP %s indisponible: %s", name, exc)

    async def call(self, qualified_name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(qualified_name)
        if tool is None or tool.server not in self._sessions:
            return {"error": "mcp_tool_unavailable"}
        res = await asyncio.wait_for(self._sessions[tool.server].call_tool(tool.name, args), 8)
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        # noms snake_case (SDK ≥ 2) ou camelCase (SDK 1.x)
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        is_error = getattr(res, "is_error", None) if hasattr(res, "is_error") else getattr(res, "isError", False)
        return {"result": structured if structured else "\n".join(texts), "is_error": bool(is_error)}

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._stack.aclose()
        self._sessions.clear()
        self.tools.clear()


mcp_manager = McpManager()
