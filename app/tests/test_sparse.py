from rag import query_prep, sparse
from rag.vectorstore import Hit


def test_terms_are_folded_stemmed_and_keep_codes():
    terms = sparse.terms("Artículo R.12: el formulario de reembolso; período anual-mensual", "es")
    assert "r.12" in terms and "12" in terms
    assert "anual-mensual" in terms  # the compound whole, besides its stemmed parts
    assert "el" not in terms and "de" not in terms  # stopwords
    assert sparse.terms("Reembolsos", "es")[0] == sparse.terms("reembolso", "es")[0]  # same stem


def test_bm25_weights_saturate_and_ids_are_stable():
    ids, values = sparse.document_vector("refund refund refund policy", "en")
    weights = dict(zip(ids, values))
    refund, policy = sparse.term_id(sparse.terms("refund", "en")[0]), sparse.term_id("polici")
    assert weights[refund] > weights[policy]
    assert weights[refund] < 3 * weights[policy]  # term frequency saturates (k1)
    assert sparse.query_vector("refund policy refund", "en")[1] == [1.0, 1.0]


def hit(score, doc):
    return Hit(score=score, text="t", payload={"doc_id": doc, "chunk_index": 0, "title": doc})


def test_rank_only_lists_count_by_rank_and_are_never_cut_by_score():
    dense = [hit(0.9, "a")]
    bm25 = [hit(17.0, "b"), hit(3.0, "c")]
    out = query_prep.fuse([dense], top_k=5, min_score=0.45, score_margin=0.1, max_per_doc=2, rank_only=[bm25])
    assert {h.payload["doc_id"] for h in out} == {"a", "b", "c"}
    assert next(h for h in out if h.payload["doc_id"] == "a").score == 0.9
