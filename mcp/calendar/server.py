"""Serveur MCP calendrier d'exemple : l'agent réserve via `book_appointment` (détecté automatiquement).

Brancher : MCP_SERVERS_JSON='{"calendar": {"command": "python", "args": ["mcp/calendar/server.py"]}}'
"""
from __future__ import annotations

from datetime import datetime, timedelta

try:  # SDK mcp ≥ 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # SDK mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("callwiz-calendar")
_BOOKED: list[dict] = []


@mcp.tool()
def list_slots(days: int = 3) -> list[str]:
    """Créneaux libres des prochains jours (10h, 14h, 16h)."""
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    slots = [(base + timedelta(days=d)).replace(hour=h) for d in range(1, days + 1) for h in (10, 14, 16)]
    taken = {b["slot"] for b in _BOOKED}
    return [s.strftime("%A %d/%m %Hh") for s in slots if s.strftime("%A %d/%m %Hh") not in taken]


@mcp.tool()
def book_appointment(preferred_time: str, phone: str = "", reason: str = "", name: str = "", organization_id: str = "") -> dict:
    """Réserve le créneau demandé (ou le premier libre)."""
    free = list_slots()
    slot = next((s for s in free if preferred_time.lower() in s.lower()), free[0] if free else preferred_time)
    _BOOKED.append({"slot": slot, "phone": phone, "reason": reason, "name": name})
    return {"booked": True, "slot": slot}


if __name__ == "__main__":
    mcp.run()
