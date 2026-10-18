import json

from rag import admin, branding


def control(tmp_path, ui=None, icon=None):
    c = tmp_path / "_control"
    c.mkdir()
    if ui is not None:
        (c / "ui.json").write_text(json.dumps(ui), encoding="utf-8")
    if icon:
        (c / icon).write_bytes(b"<svg/>")
    return tmp_path


def test_defaults_without_control_folder(tmp_path):
    brand = branding.load(str(tmp_path), "_control", "es")
    assert brand["name"] == "RAG Documental" and brand["suggestions"] == [] and brand["icon"] is False


def test_ui_json_overrides_and_is_validated(tmp_path):
    root = control(tmp_path, {"name": "Normas", "suggestions": ["a", " ", "b"], "welcome_text": 3,
                              "colors": {"accent": "#f5a623", "accent_text": "red"}, "icon": "logo.svg"},
                   icon="logo.svg")
    brand = branding.load(str(root), "_control", "en")
    assert brand["name"] == "Normas" and brand["suggestions"] == ["a", "b"]
    assert brand["welcome_text"] == branding.DEFAULTS["en"]["welcome_text"]
    assert brand["colors"] == {"accent": "#f5a623"}
    assert brand["icon"] is True
    assert branding.manifest(brand, "/api/branding/icon")["theme_color"] == "#f5a623"


def test_context_fields_are_validated(tmp_path):
    root = control(tmp_path, {"context_fields": [
        {"id": "plan", "label": "Plan", "options": ["Basic", "Basic", "Premium", ""]},
        {"id": "region", "label": "Region", "field": "jurisdiction", "boost": True, "exclude_options": ["All"],
         "placeholder": "Any region"},
        {"id": "plan", "label": "Duplicate", "options": ["x"]},
        {"id": "Bad id", "label": "x", "options": ["x"]},
        {"id": "secret", "label": "Not indexed", "field": "password", "boost": True},
        {"id": "fixed", "label": "Fixed", "options": ["a"], "boost": True},
    ]})
    fields = branding.load(str(root), "_control", "en")["context_fields"]
    assert [f["id"] for f in fields] == ["plan", "region", "fixed"]
    assert fields[0]["options"] == ["Basic", "Premium"] and fields[0]["boost"] is False
    assert fields[1]["field"] == "jurisdiction" and fields[1]["boost"] is True
    assert fields[1]["exclude_options"] == ["All"] and fields[1]["placeholder"] == "Any region"
    assert fields[2]["boost"] is False  # boosting needs an indexed field


def test_selector_fields_come_from_the_collection_config(tmp_path):
    root = control(tmp_path, {"context_fields": [{"id": "team", "label": "Team", "field": "topic", "boost": True}]})
    assert branding.load(str(root), "_control", "en")["context_fields"] == []  # topic is not indexed by default
    (root / "_control" / "control_config.json").write_text(json.dumps({"index_fields": ["status", "topic", "text"]}),
                                                          encoding="utf-8")
    fields = branding.load(str(root), "_control", "en")["context_fields"]
    assert fields[0]["field"] == "topic" and fields[0]["boost"] is True
    from rag.control import ControlFolder
    assert ControlFolder.load(root, "_control").index_fields == ("status", "topic")  # "text" is reserved


def test_icon_outside_the_control_folder_is_ignored(tmp_path):
    (tmp_path / "secret.svg").write_text("<svg/>", encoding="utf-8")
    root = control(tmp_path, {"icon": "../secret.svg"})
    assert branding.icon_path(str(root), "_control") is None


def test_admin_token_or_this_machine():
    assert admin.authorized("s3cret", "s3cret", "203.0.113.5")
    assert not admin.authorized("s3cret", "wrong", "127.0.0.1")
    assert not admin.authorized("s3cret", None, "127.0.0.1")
    # Without a token: loopback or the Docker host (bridge gateway) only
    assert admin.authorized("", None, "127.0.0.1", gateway="172.18.0.1")
    assert admin.authorized("", None, "172.18.0.1", gateway="172.18.0.1")
    assert not admin.authorized("", None, "172.18.0.7", gateway="172.18.0.1")
    assert not admin.authorized("", None, None, gateway="172.18.0.1")
