from rag import query_prep
from rag.prompts import build_user_message, labels
from rag.vectorstore import Hit


def hit(score, doc, chunk=0):
    return Hit(score=score, text="t", payload={"doc_id": doc, "chunk_index": chunk, "title": doc})


def test_model_output_is_validated_and_falls_back_to_the_question():
    p = query_prep.from_model(None, "vacation polcy", 3)
    assert not p.ok and p.search_queries(3) == ["vacation polcy"]
    p = query_prep.from_model({"corrected": "  ", "standalone": 5, "queries": ["a", "", 7, "b", "c", "d"],
                               "missing": "x"}, "q", 3)
    assert p.ok and p.corrected == "q" and p.standalone == "q"
    assert p.queries == ["a", "b", "c"] and p.missing == []


def test_search_queries_start_with_the_original_and_drop_duplicates():
    p = query_prep.from_model({"corrected": "vacation policy", "standalone": "vacation policy for new staff",
                               "queries": ["VACATION POLICY FOR NEW STAFF", "annual leave", "probation period"],
                               "missing": []}, "vacation polcy new staff", 3)
    assert p.search_queries(3) == ["vacation polcy new staff", "vacation policy for new staff", "annual leave",
                                   "probation period"]
    assert len(p.search_queries(1)) == 3


def test_fusion_rewards_agreement_and_caps_chunks_per_document():
    first = [hit(0.9, "a", 0), hit(0.85, "a", 1), hit(0.84, "a", 2), hit(0.8, "b")]
    second = [hit(0.7, "b"), hit(0.69, "c")]
    out = query_prep.fuse([first, second], top_k=10, min_score=0.3, score_margin=0.2, max_per_doc=2)
    keys = [(h.payload["doc_id"], h.payload["chunk_index"]) for h in out]
    assert keys[0] == ("b", 0)  # found by both searches
    assert sum(1 for d, _ in keys if d == "a") == 2
    assert ("c", 0) in keys


def test_every_search_keeps_its_best_hit():
    shared = [hit(0.9, "a", 0), hit(0.89, "a", 1), hit(0.88, "b", 0), hit(0.87, "b", 1)]
    keyword = [hit(0.6, "k")]
    out = query_prep.fuse([shared, list(shared), keyword], top_k=3, min_score=0.3, score_margin=0.2,
                          max_per_doc=2)
    assert "k" in [h.payload["doc_id"] for h in out]
    assert out[0].payload["doc_id"] == "a"  # the most supported chunk still comes first


def test_keywords_are_deduplicated_and_capped():
    p = query_prep.from_model({"corrected": "q", "standalone": "q", "queries": [],
                               "keywords": ["Form", "form", "annex", "x" * 200, "four"], "missing": []}, "q", 3, 3)
    assert p.keywords == ["Form", "annex", "x" * 80]
    assert query_prep.from_model({"keywords": ["a"]}, "q", 3).keywords == []


def test_fusion_cuts_weak_hits_of_each_search():
    out = query_prep.fuse([[hit(0.9, "a"), hit(0.5, "b")], [hit(0.2, "c")]], top_k=10, min_score=0.3,
                          score_margin=0.15, max_per_doc=2)
    assert [h.payload["doc_id"] for h in out] == ["a"]


def test_answer_message_shows_the_interpretation_only_when_it_adds_something():
    docs = [{"n": 1, "title": "Handbook", "location": "handbook.pdf", "url": "", "source": "folder", "source_label": "Local folder", "sections": [],
             "texts": ["texto"], "status": "", "valid_until": "", "jurisdiction": ""}]
    same = query_prep.from_model({"corrected": "q", "standalone": "q", "queries": [], "missing": []}, "q", 3)
    assert labels("en")["interpretation"] not in build_user_message("q", docs, "en", same)
    other = query_prep.from_model({"corrected": "", "standalone": "annual leave for new staff",
                                   "queries": ["annual leave"], "missing": ["contract type"]}, "levae", 3)
    msg = build_user_message("levae", docs, "en", other)
    assert labels("en")["interpretation"] in msg
    assert "annual leave for new staff" in msg and "annual leave" in msg and "contract type" in msg
