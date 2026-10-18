import re

from bench.llm_benchmark import context_for, detect_language, load_dataset, score

REFUSAL = [re.compile(r"no (encuentro|aparece)")]


def test_dataset_loads_and_context_is_shuffled_per_case():
    meta, docs = load_dataset("es")
    assert meta["language"] == "es"
    assert len(docs) >= 9
    assert all(c["answerable"] in (True, False, "partial") for c in meta["cases"])
    first, second = context_for(0, docs, "es"), context_for(1, docs, "es")
    assert [d["n"] for d in first] == list(range(1, len(docs) + 1))
    assert [d["title"] for d in first] != [d["title"] for d in second]


def test_facts_are_accent_and_case_insensitive():
    case = {"answerable": True, "facts": [["miércoles"], ["21:00"]]}
    s = score(case, "Los despliegues son los MIERCOLES desde las 21:00 [1].", REFUSAL)
    assert s["facts"] == 1
    assert s["correct"]


def test_forbidden_text_fails_an_answerable_case():
    case = {"answerable": True, "facts": [["4410"]], "forbidden": ["9999"]}
    assert not score(case, "Llama a la 9999 o a la 4410 [1].", REFUSAL)["correct"]
    assert score(case, "Llama a la extensión 4410 [1].", REFUSAL)["correct"]


def test_unanswerable_needs_a_refusal():
    case = {"answerable": False, "forbidden": ["millones"]}
    assert score(case, "No encuentro esa información en los documentos.", REFUSAL)["correct"]
    assert not score(case, "Gana 8 millones al mes.", REFUSAL)["correct"]


def test_answer_language():
    assert detect_language("Employees get 15 days per year and they must request them 20 days ahead.") == "en"
    assert detect_language("Los colaboradores tienen 15 días por año y se piden con 20 días.") == "es"
    case = {"answerable": True, "facts": [["15"]], "expect_language": "en"}
    assert not score(case, "Tienes 15 días de vacaciones por año [1].", REFUSAL)["correct"]
