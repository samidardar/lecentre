from __future__ import annotations

from app.rag.chunking import chunk_text, count_tokens
from app.rag.parsing import extract_text
from app.rag.retrieval import BM25Index, rrf
from tests.conftest import FAQ


def test_chunking_keeps_qa_pairs_and_sections() -> None:
    chunks = chunk_text(FAQ)
    qa = [c for c in chunks if c.kind == "qa"]
    assert len(qa) == 3
    retour = next(c for c in qa if "retour" in c.content)
    assert "30 jours" in retour.content and retour.section == "Informations pratiques"
    assert all(count_tokens(c.content) <= 650 for c in chunks)


def test_long_text_is_split_with_bounded_size() -> None:
    text = "# Guide\n\n" + "\n\n".join(f"Paragraphe {i}. " + "Mot " * 120 for i in range(12))
    chunks = chunk_text(text)
    assert len(chunks) > 2 and all(count_tokens(c.content) <= 700 for c in chunks)


def test_csv_faq_parsing() -> None:
    data = "question;réponse\nQuels moyens de paiement ?;Carte bancaire et virement.\n".encode()
    text = extract_text("faq.csv", data)
    assert "Q: Quels moyens de paiement ?" in text and "R: Carte bancaire" in text


def test_html_parsing() -> None:
    text = extract_text("page.html", b"<html><script>x()</script><h2>Livraison</h2><p>Sous 48h.</p></html>")
    assert "## Livraison" in text and "x()" not in text


def test_bm25_and_rrf() -> None:
    idx = BM25Index()
    idx.add("a", "politique de retour sous 30 jours", {"knowledge_base_id": "kb"})
    idx.add("b", "horaires d'ouverture du magasin", {"knowledge_base_id": "kb"})
    res = idx.search("retour produit", 5)
    assert res[0][0] == "a" and res[0][2] == 1
    fused = rrf([["a", "b"], ["b"]])
    assert fused["b"] > fused["a"]
