from pathlib import Path

import pytest

from rag.ingest.chunker import chunk_text
from rag.ingest.loaders import UnsupportedFormat, extract, html_to_text


def test_chunker_respects_size_and_overlap():
    text = "\n\n".join(f"Párrafo {i}. " + "palabra " * 80 for i in range(20))
    chunks = chunk_text(text, size=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 + 100 + 1 for c in chunks)
    assert "Párrafo 0." in chunks[0]
    assert "Párrafo 19." in chunks[-1]


def test_chunker_short_text_single_chunk():
    assert chunk_text("hola mundo", size=100, overlap=10) == ["hola mundo"]
    assert chunk_text("   ", size=100) == []


def test_html_to_text_keeps_structure():
    html = "<h2>Título</h2><p>Uno</p><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table><ul><li>x</li></ul>"
    text = html_to_text(html)
    assert "## Título" in text
    assert "A | B" in text and "1 | 2" in text
    assert "- x" in text


def test_text_and_unknown_extension(tmp_path: Path):
    f = tmp_path / "config.conf"
    f.write_text("clave=valor\nservidor=interno", encoding="utf-8")
    assert "servidor=interno" in extract(f)[0].text


def test_csv(tmp_path: Path):
    f = tmp_path / "datos.csv"
    f.write_text("nombre;puerto\nqdrant;6333\nollama;11434\n", encoding="utf-8")
    text = extract(f)[0].text
    assert "nombre: qdrant; puerto: 6333" in text


def test_docx(tmp_path: Path):
    import docx

    d = docx.Document()
    d.add_heading("Procedimiento", level=1)
    d.add_paragraph("Paso uno del despliegue")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Ambiente", "URL"
    t.cell(1, 0).text, t.cell(1, 1).text = "QA", "qa.local"
    path = tmp_path / "doc.docx"
    d.save(path)
    text = extract(path)[0].text
    assert "# Procedimiento" in text
    assert "Paso uno del despliegue" in text
    assert "Ambiente: QA; URL: qa.local" in text


def test_binary_is_unsupported(tmp_path: Path):
    f = tmp_path / "archivo.xyz"
    f.write_bytes(bytes(range(256)) * 10)
    with pytest.raises(UnsupportedFormat):
        extract(f)


def test_image_without_ocr_is_unsupported(tmp_path: Path):
    f = tmp_path / "captura.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(UnsupportedFormat):
        extract(f)
