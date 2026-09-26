"""Serveur MCP CRM d'exemple (FastMCP, stdio). À remplacer par le connecteur du CRM client (HubSpot, Salesforce…).

Brancher : MCP_SERVERS_JSON='{"crm": {"command": "python", "args": ["mcp/crm/server.py"]}}'
Outils exposés aux agents : mcp_crm_lookup_contact, mcp_crm_log_call.
"""
from __future__ import annotations

try:  # SDK mcp ≥ 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # SDK mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("callwiz-crm")
_CONTACTS = {"+33612345678": {"name": "Alice Martin", "customer_since": "2023", "tier": "gold", "open_ticket": "Réparation frein #4521"}}
_LOG: list[dict] = []


@mcp.tool()
def lookup_contact(phone: str) -> dict:
    """Retourne la fiche client associée à un numéro E.164 (ou found=false)."""
    c = _CONTACTS.get(phone)
    return {"found": bool(c), **(c or {})}


@mcp.tool()
def log_call(phone: str, summary: str, outcome: str) -> dict:
    """Journalise un appel dans le CRM."""
    _LOG.append({"phone": phone, "summary": summary, "outcome": outcome})
    return {"ok": True, "count": len(_LOG)}


if __name__ == "__main__":
    mcp.run()
