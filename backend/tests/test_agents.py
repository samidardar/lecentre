from __future__ import annotations

from app.agents.context import CallContext, CallRuntime
from app.agents.graphs import get_graphs
from app.agents.inbound import inbound_agent
from app.agents.outbound import outbound_agent
from app.agents.supervisor import supervisor


def make_ctx(**kw) -> CallContext:  # type: ignore[no-untyped-def]
    base = dict(call_id="00000000-0000-0000-0000-000000000001", organization_id="00000000-0000-0000-0000-0000000000aa",
                direction="inbound", org_name="Acme", org_description="Magasin de vélos", tone="chaleureux", language="fr",
                voice="Kore", timezone="Europe/Paris", from_number="+33600000000", to_number="+33100000000",
                allowed_tools=["search_knowledge_base", "take_message", "transfer_to_human"], transfer_number="+33199999999")
    base.update(kw)
    return CallContext(**base)  # type: ignore[arg-type]


def test_route_inbound_and_outbound() -> None:
    d = supervisor.route(make_ctx())
    assert d.route == "inbound_agent" and "end_call" in d.tools and "set_outcome" in d.tools
    assert d.fallback == "transfer_human"
    d2 = supervisor.route(make_ctx(direction="outbound", transfer_number=None, allowed_tools=["search_knowledge_base", "transfer_to_human"]))
    assert d2.route == "outbound_agent" and "transfer_to_human" not in d2.tools


def test_instructions_contain_disclosure_and_personalization() -> None:
    ctx = make_ctx(direction="outbound", objective="confirm_appointment", script="Rappeler le RDV de {first_name} le {date_rdv}.",
                   contact={"first_name": "Alice", "date_rdv": "mardi"})
    plan = outbound_agent.plan(ctx, ["search_knowledge_base", "end_call"], [{"content": "Horaires 9h-18h"}])
    assert "Alice" in plan.instructions and "mardi" in plan.instructions
    assert "assistant virtuel" in plan.instructions and "Horaires 9h-18h" in plan.instructions
    assert "Alice" in plan.greeting
    inbound = inbound_agent.plan(make_ctx(greeting="Bonjour, Acme j'écoute"), ["end_call"], [])
    assert "Bonjour, Acme j'écoute" in inbound.kickoff


def test_supervisor_opt_out_and_hallucination() -> None:
    rt = CallRuntime(ctx=make_ctx())
    rt.turn_count = 2
    a = supervisor.review_turn(rt, "Ne m'appelez plus jamais", "Très bien.", [])
    assert a.action == "steer" and a.urgent and rt.outcome["status"] == "opted_out"

    rt2 = CallRuntime(ctx=make_ctx())
    rt2.turn_count = 2
    rt2.rag_hits.append({"content": "L'abonnement coûte 29 euros par mois."})
    ok = supervisor.review_turn(rt2, "Combien ça coûte ?", "L'abonnement coûte 29 euros par mois.", [])
    assert ok.action == "none"
    bad = supervisor.review_turn(rt2, "Et le délai ?", "La livraison prend 3 jours et coûte 12 euros.", [])
    assert bad.action == "steer" and bad.reason == "hallucination_suspected"
    assert set(bad.data["claims"]) == {"3 jours", "12 euros"}


def test_supervisor_disclosure_and_escalation() -> None:
    rt = CallRuntime(ctx=make_ctx())
    rt.turn_count = 1
    assert supervisor.review_turn(rt, "", "Bonjour, que puis-je faire ?", []).reason == "disclosure_missing"
    rt.turn_count = 3
    supervisor.review_turn(rt, "Je veux parler à un conseiller", "D'accord.", [])
    a = supervisor.review_turn(rt, "Passez-moi un conseiller !", "Je comprends.", [])
    assert a.action in ("transfer", "steer")


async def test_tool_graph_authorization_and_rag() -> None:
    graphs = get_graphs()
    rt = CallRuntime(ctx=make_ctx())
    denied = await graphs.tool.ainvoke({"rt": rt, "enabled": ["end_call"], "name": "book_appointment", "args": {"preferred_time": "demain"}})
    assert denied["allowed"] is False and denied["result"]["error"] == "refused"
    end = await graphs.tool.ainvoke({"rt": rt, "enabled": ["end_call"], "name": "end_call", "args": {"reason": "fin"}})
    assert end["allowed"] and rt.pending_action == {"type": "end", "reason": "fin"}
    rag = await graphs.tool.ainvoke({"rt": rt, "enabled": ["search_knowledge_base"], "name": "search_knowledge_base", "args": {"query": "horaires"}})
    assert rag["result"]["low_confidence"] is True  # base vide → pas d'invention


async def test_transfer_requires_open_hours() -> None:
    rt = CallRuntime(ctx=make_ctx(business_hours={"mon": ["00:00", "00:01"]}))  # quasi toujours fermé
    res = await get_graphs().tool.ainvoke({"rt": rt, "enabled": ["transfer_to_human"], "name": "transfer_to_human", "args": {"reason": "x"}})
    assert res["result"]["transferred"] is False and rt.pending_action is None
