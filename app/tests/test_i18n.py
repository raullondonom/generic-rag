from rag.i18n import TEXTS, normalize, t


def test_every_language_has_every_key():
    for language, texts in TEXTS.items():
        assert set(texts) == set(TEXTS["en"]), language


def test_placeholders_named_like_parameters():
    # Texts may use any placeholder name, even "key" or "language" (the parameters of t())
    assert t("en", "sheet", name="Sales", key="ignored", language="ignored") == "sheet Sales"
    assert t("es", "sheet", name="Ventas") == "hoja Ventas"


def test_unknown_language_falls_back_to_english():
    assert normalize("fr") == "en"
    assert normalize("es-CO") == "es"
    assert t("fr", "local_folder") == "Local folder"
