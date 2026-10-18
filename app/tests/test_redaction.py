import pytest

from rag.redaction import DEFAULT_MARKER, redact

# Fake credentials are built from pieces so secret scanners (gitleaks, GitHub) do not flag the repo
HIGH_ENTROPY_VALUE = "abcdEFGH1234" + "ijklMNOP5678" + "qrstUVWX"
FAKE_SECRETS = [
    "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789AB",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"]),
]


@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_known_tokens_are_redacted(secret):
    result = redact(f"The value is {secret} and the text goes on")
    assert secret not in result.text
    assert DEFAULT_MARKER in result.text
    assert result.total >= 1


def test_key_value_and_connection_strings():
    text = ("DB_PASSWORD=Hun7er2Secret\n"
            "Server=db01;Database=sales;User Id=sa;Password=Key123!;\n"
            "postgresql://admin:MyKey99@db:5432/app")
    out = redact(text).text
    for leaked in ("Hun7er2Secret", "Key123!", "MyKey99"):
        assert leaked not in out


def test_spanish_wording_is_recognized():
    out = redact(f"La contraseña es Pr1vad0Clave y el token: {HIGH_ENTROPY_VALUE}").text
    assert "Pr1vad0Clave" not in out
    assert HIGH_ENTROPY_VALUE not in out


def test_custom_marker():
    assert redact("password=Hun7er2Secret", marker="[REDACTADO]").text == "password=[REDACTADO]"


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"key:\n{pem}\nend").text == f"key:\n{DEFAULT_MARKER}\nend"


def test_normal_prose_is_untouched():
    text = ("Deployment runs through the release pipeline. Model tokens are counted per request and "
            "the keyword is 'deploy'. Contact: support@company.com")
    assert redact(text).text == text


def test_personal_data_only_when_enabled():
    text = "Write to ana.perez@company.com or call phone 300 123 4567"
    assert redact(text).text == text
    out = redact(text, personal=True).text
    assert "ana.perez@company.com" not in out
    assert "300 123 4567" not in out


def test_disabled_returns_text_unchanged():
    text = "password=Hun7er2Secret"
    assert redact(text, secrets=False, personal=False).text == text


def test_idempotent():
    once = redact(f"token: {HIGH_ENTROPY_VALUE}").text
    assert redact(once).text == once
