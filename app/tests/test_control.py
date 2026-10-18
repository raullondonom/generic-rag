import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from rag.config import Settings
from rag.control import ControlFolder, RemoteChecker, document_metadata, first_url, metadata_version, review
from rag.ingest.folder_source import FolderSource, context_lines
from rag.ingest.loaders import Section
from rag.ingest.pipeline import Indexer

TODAY = dt.date(2026, 10, 18)
ES_HEADER = ("archivo;titulo;tipo_norma;numero;anio;entidad_emisora;ambito;jurisdiccion;tema;url_descarga;"
             "url_consulta;metodo;fecha_expedicion;fecha_descarga;estado_vigencia;vigencia_hasta;"
             "frecuencia_revision;como_detectar_cambios;observaciones")
ALIASES = {"archivo": "file", "titulo": "title", "tipo_norma": "doc_type", "numero": "number", "anio": "year",
           "entidad_emisora": "issuer", "ambito": "scope", "jurisdiccion": "jurisdiction", "tema": "topic",
           "url_descarga": "download_url", "url_consulta": "reference_url", "metodo": "method",
           "fecha_expedicion": "issue_date", "fecha_descarga": "retrieved_date", "estado_vigencia": "status",
           "vigencia_hasta": "valid_until", "frecuencia_revision": "review_frequency",
           "como_detectar_cambios": "change_check", "observaciones": "notes"}


def es_row(file="", title="", jurisdiction="", download="", reference="", retrieved="", status="",
           until="", frequency="", check=""):
    return ";".join([file, title, "decreto", "1", "2026", "Alcaldía", "municipal", jurisdiction, "", download,
                     reference, "pdf_directo", "2026-01-01", retrieved, status, until, frequency, check, ""])


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    control = tmp_path / "_control"
    control.mkdir()
    (control / "control_config.json").write_text(json.dumps({
        "registry_globs": ["fuentes_*.csv", "sources_*.csv"], "column_aliases": ALIASES,
        "review_periods_days": {"anual": 365, "semestral": 182},
        "excluded_dirs": ["_control", "_archive", "_historico"],
    }), encoding="utf-8")
    rows = [
        es_row("01_laws/law_1.pdf", "Ley 1", "Nacional", "https://x.gov/law1.pdf", "https://x.gov/laws",
               "2026-01-01", "vigente", "", "anual"),
        es_row("05_local/antioquia/barbosa/decree.pdf", "Decreto Barbosa", "Barbosa", "", "https://b.gov (lista)",
               "2026-10-01", "temporal", "2026-10-01", "semestral", "Buscar el decreto 2027"),
        es_row("05_local/santander/barbosa/decree.pdf", "Decreto Barbosa (Santander)", "Barbosa (Santander)", "", "",
               "2026-10-01", "temporal", "2026-11-05", "semestral"),
        es_row("02_decrees/missing.pdf", "No está", "Nacional", "", "", "2026-10-01", "vigente", "", "anual"),
        es_row("", "", "Mitú", "", "https://mitu.gov", "", "", "", "anual"),
        es_row("02_decrees/old.pdf", "Viejo", "Nacional", "", "", "2025-01-01", "vigente", "", "semestral"),
    ]
    (control / "fuentes_a.csv").write_text("﻿" + ES_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    # A later registry in English with a duplicate of the first row (must be skipped: the first one wins)
    (control / "sources_b.csv").write_text(
        "file;title;status\n01_LAWS/law_1.pdf;Duplicate;in_force\n", encoding="utf-8")
    (control / "sources_master.csv").write_text("file;title\nignored.pdf;x\n", encoding="utf-8")
    for rel in ("01_laws/law_1.pdf", "05_local/antioquia/barbosa/decree.pdf", "05_local/santander/barbosa/decree.pdf",
                "02_decrees/old.pdf", "03_case_law/unregistered.pdf", "_archive/02_decrees/older.pdf",
                "06_source_inventory/report.pdf"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 " + rel.encode())
    (tmp_path / "_inventory.csv").write_text("a;b\n1;2\n", encoding="utf-8")
    return tmp_path


def test_registries_are_merged_with_aliases_and_deduplicated(corpus):
    folder = ControlFolder.load(corpus, "_control")
    rows = folder.rows()
    assert [p.name for p in folder.registry_files()] == ["fuentes_a.csv", "sources_b.csv"]
    assert len(rows) == 6  # the English duplicate is skipped, the master file is ignored
    lookup = folder.lookup()
    row = lookup["05_local/antioquia/barbosa/decree.pdf"]
    assert row["title"] == "Decreto Barbosa"
    assert row["registry"] == "fuentes_a.csv"
    assert "02_decrees/old.pdf" in lookup


def test_no_control_folder():
    assert ControlFolder.load("/does/not/exist", "_control") is None


def test_metadata_for_retrieval():
    row = {"title": "Decreto", "status": "Vigente", "jurisdiction": "Popayán", "valid_until": "2026-12-31",
           "reference_url": "https://p.gov.co/normas (lista SharePoint)", "download_url": "https://p.gov.co/d.pdf"}
    meta = document_metadata(row)
    assert meta["status"] == "vigente"
    assert meta["url"] == "https://p.gov.co/normas"
    assert metadata_version(meta) == metadata_version(dict(meta))
    assert metadata_version(meta) != metadata_version({**meta, "status": "derogada"})
    assert first_url("see https://a.b/c, then") == "https://a.b/c"
    assert document_metadata(None) == {}


def test_offline_review(corpus):
    result = review(ControlFolder.load(corpus, "_control"), online=False, today=TODAY)
    by_section = {}
    for f in result["findings"]:
        by_section.setdefault(f["section"], []).append(f.get("file") or f.get("jurisdiction"))
    assert by_section["expired"] == ["05_local/antioquia/barbosa/decree.pdf"]
    assert by_section["expiring"] == ["05_local/santander/barbosa/decree.pdf"]
    assert by_section["missing"] == ["02_decrees/missing.pdf"]
    assert by_section["no_document"] == ["Mitú"]
    assert by_section["review_due"] == ["02_decrees/old.pdf"]
    # _archive is excluded; files in other folders without a row are reported
    assert sorted(by_section["unregistered"]) == ["03_case_law/unregistered.pdf", "06_source_inventory/report.pdf"]
    assert result["counts"]["changed"] == 0
    expired = next(f for f in result["findings"] if f["section"] == "expired")
    assert expired["hint"] == "Buscar el decreto 2027"
    assert expired["date"] == "2026-10-01"


def test_remote_review_detects_changes_from_the_second_run(corpus):
    content = {"body": b"version 1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v2"':
            return httpx.Response(304)
        etag = '"v2"' if content["body"] == b"version 2" else '"v1"'
        return httpx.Response(200, content=content["body"], headers={"content-type": "application/pdf", "etag": etag})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    checker = RemoteChecker("test", 5, client=client, insecure_client=client)
    folder = ControlFolder.load(corpus, "_control")

    first = review(folder, {}, online=True, checker=checker, today=TODAY)
    assert first["counts"]["changed"] == 0  # the first check only stores fingerprints
    state = first["remote_state"]
    assert set(state) == {"https://x.gov/law1.pdf"}

    content["body"] = b"version 2"
    second = review(folder, state, online=True, checker=checker, today=TODAY + dt.timedelta(days=1))
    changed = [f for f in second["findings"] if f["section"] == "changed"]
    assert [f["url"] for f in changed] == ["https://x.gov/law1.pdf"]
    assert changed[0]["date"] == TODAY.isoformat()

    # Unchanged (304 thanks to the stored ETag): no new finding
    third = review(folder, second["remote_state"], online=True, checker=checker, today=TODAY + dt.timedelta(days=2))
    assert third["counts"]["changed"] == 0


def test_unreachable_urls_are_reported(corpus):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    checker = RemoteChecker("test", 5, client=client, insecure_client=client)
    result = review(ControlFolder.load(corpus, "_control"), {}, online=True, checker=checker, today=TODAY)
    assert result["counts"]["unreachable"] == 1


def test_html_pages_are_compared_by_visible_text(corpus):
    pages = iter([b"<html><script>var t=1</script><p>Normas 2026</p></html>",
                  b"<html><script>var t=2</script><p>Normas   2026</p></html>"])
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=next(pages), headers={"content-type": "text/html"})))
    checker = RemoteChecker("test", 5, client=client, insecure_client=client)
    a = checker.fingerprint("https://x.gov/law1.pdf", None)
    b = checker.fingerprint("https://x.gov/law1.pdf", None)
    assert a["fingerprint"] == b["fingerprint"]


# ---------------------------------------------------------------- folder source and indexer

class FakeIndexer:
    def __init__(self):
        self.indexed, self.state = {}, None

    def is_current(self, doc_id, version):
        return False

    def index(self, doc_id, **kw):
        self.indexed[doc_id] = kw

    def mark(self, doc_id, **kw):
        self.indexed[doc_id] = {"marked": True, **kw}


def test_folder_skips_reserved_prefixes_and_excluded_dirs(corpus):
    source = FolderSource(Settings(_env_file=None, docs_path=str(corpus)), FakeIndexer())
    files = sorted(rel for _, rel in source._files(source.control()))
    assert "_inventory.csv" not in files
    assert not any(f.startswith(("_control/", "_archive/")) for f in files)
    assert "05_local/antioquia/barbosa/decree.pdf" in files


def test_registry_metadata_reaches_the_index(corpus, monkeypatch):
    monkeypatch.setattr("rag.ingest.folder_source.extract", lambda path, opts: [Section("texto del decreto")])
    indexer = FakeIndexer()
    source = FolderSource(Settings(_env_file=None, docs_path=str(corpus), language="es"), indexer)
    control = source.control()
    for path, rel in source._files(control):
        info = document_metadata(control.lookup().get(rel.lower()), control.index_fields)
        source._process(path, rel, info, full=False, index_fields=control.index_fields)
    kw = indexer.indexed["folder:05_local/antioquia/barbosa/decree.pdf"]
    assert kw["title"] == "Decreto Barbosa"
    assert kw["url"] == "https://b.gov"
    assert kw["extra"] == {"status": "temporal", "valid_until": "2026-10-01", "jurisdiction": "Barbosa"}
    assert kw["context"][0] == "Carpeta: 05_local/antioquia/barbosa"
    assert "Jurisdicción: Barbosa" in kw["context"][1]
    assert "Vigente hasta: 2026-10-01" in kw["context"][1]
    assert ":" in kw["version"]  # file hash + registry metadata hash
    unregistered = indexer.indexed["folder:03_case_law/unregistered.pdf"]
    assert unregistered["title"] == "unregistered.pdf"
    assert unregistered["context"] == ["Carpeta: 03_case_law"]


def test_ocr_text_replaces_its_scanned_pdf(corpus, monkeypatch):
    monkeypatch.setattr("rag.ingest.folder_source.extract", lambda path, opts: [Section("pdf text layer")])
    (corpus / "02_decrees" / "old.ocr.txt").write_text(
        "Texto obtenido por OCR de: 02_decrees/old.pdf\nMétodo: OCR\n\n--- Página 1 de 2 ---\nArtículo uno\n"
        "\n--- Página 2 de 2 ---\nArtículo dos\n", encoding="utf-8")
    (corpus / "03_case_law" / "orphan.ocr.txt").write_text("texto suelto", encoding="utf-8")
    indexer = FakeIndexer()
    source = FolderSource(Settings(_env_file=None, docs_path=str(corpus), language="es"), indexer)
    control = source.control()
    sidecar = source.sidecar_suffix(control)
    files = [rel for _, rel in source._files(control, sidecar)]
    assert "02_decrees/old.ocr.txt" not in files  # consumed by its PDF
    assert "03_case_law/orphan.ocr.txt" in files  # no PDF beside it: indexed as plain text
    pdf = corpus / "02_decrees" / "old.pdf"
    source._process(pdf, "02_decrees/old.pdf", document_metadata(control.lookup()["02_decrees/old.pdf"]), False, sidecar)
    kw = indexer.indexed["folder:02_decrees/old.pdf"]
    assert [(s.location, s.text) for s in kw["sections"]] == [("página 1", "Artículo uno"), ("página 2", "Artículo dos")]
    assert kw["context"][-1].startswith("Texto obtenido por OCR")
    assert "+ocr2:" in kw["version"]
    assert kw["location"] == "02_decrees/old.pdf"  # the citation opens the original PDF


def test_ocr_text_with_windows_line_endings():
    from rag.ingest.folder_source import ocr_sections

    text = ("Texto obtenido por OCR de: a.pdf\r\nMétodo: OCR\r\n\r\n--- Página 1 de 3 ---\r\nuno\r\n"
            "\r\n--- Página 2 de 3 ---\r\ndos\r\n\r\n--- Página 3 de 3 ---\r\ntres\r\n")
    assert [(s.location, s.text) for s in ocr_sections(text, "es")] == [
        ("página 1", "uno"), ("página 2", "dos"), ("página 3", "tres")]


def test_empty_ocr_text_falls_back_to_the_pdf(corpus, monkeypatch):
    monkeypatch.setattr("rag.ingest.folder_source.extract", lambda path, opts: [Section("pdf text layer")])
    (corpus / "02_decrees" / "old.ocr.txt").write_text("Texto obtenido por OCR de: x\n", encoding="utf-8")
    indexer = FakeIndexer()
    source = FolderSource(Settings(_env_file=None, docs_path=str(corpus)), indexer)
    source._process(corpus / "02_decrees" / "old.pdf", "02_decrees/old.pdf", {}, False, ".ocr.txt")
    kw = indexer.indexed["folder:02_decrees/old.pdf"]
    assert [s.text for s in kw["sections"]] == ["pdf text layer"]
    assert not any("OCR" in line for line in kw["context"])


def test_ocr_sidecar_can_be_turned_off(corpus):
    source = FolderSource(Settings(_env_file=None, docs_path=str(corpus), ocr_sidecar_suffix=""), FakeIndexer())
    assert source.sidecar_suffix(None) == ""


def test_assistant_notes(corpus):
    config = json.loads((corpus / "_control" / "control_config.json").read_text(encoding="utf-8"))
    config["assistant_notes_file"] = "notes.md"
    (corpus / "_control" / "control_config.json").write_text(json.dumps(config), encoding="utf-8")
    folder = ControlFolder.load(corpus, "_control")
    assert folder.assistant_notes() == ""  # missing file: no notes
    (corpus / "_control" / "notes.md").write_text("- La norma nacional prevalece.\n", encoding="utf-8")
    assert folder.assistant_notes() == "- La norma nacional prevalece."
    config["assistant_notes_file"] = "../01_laws/law_1.pdf"  # outside the control folder: ignored
    (corpus / "_control" / "control_config.json").write_text(json.dumps(config), encoding="utf-8")
    assert ControlFolder.load(corpus, "_control").assistant_notes() == ""


def test_context_lines_without_registry():
    assert context_lines("guide.md", {}, "en") == []
    assert context_lines("a/b/c.pdf", {"status": "in_force"}, "en") == ["Folder: a/b", "Status: in_force"]


class Capture:
    def __init__(self):
        self.chunks, self.payloads = [], []

    def embed_documents(self, chunks):
        return [[0.0] for _ in chunks]

    def delete_doc(self, doc_id):
        pass

    def upsert(self, doc_id, chunks, vectors, payloads, sparse=None):
        self.chunks, self.payloads = chunks, payloads


class NullState:
    def upsert(self, *a, **kw):
        pass


def test_indexer_puts_context_in_chunks_and_extra_in_payload():
    capture = Capture()
    indexer = Indexer(Settings(_env_file=None), capture, capture, NullState())
    indexer.index("folder:a.pdf", source="folder", title="Decreto 1", location="a.pdf", url="https://x",
                  version="v", sections=[Section("cuerpo", "page 1")], context=["Folder: 05/x"],
                  extra={"status": "vigente"})
    assert capture.chunks[0].startswith("Decreto 1 (page 1)\nFolder: 05/x\n\ncuerpo")
    assert capture.payloads[0]["status"] == "vigente"
    assert capture.payloads[0]["url"] == "https://x"
