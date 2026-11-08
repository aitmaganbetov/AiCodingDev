import json
import logging

import pytest

from aicd_platform import context
from aicd_platform.logging import JsonFormatter
from aicd_platform.redaction import REDACTED, redact_text, redact_value


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_known_token_formats_are_redacted(secret: str) -> None:
    out = redact_text(f"prefix {secret} suffix")
    assert secret not in out
    assert REDACTED in out


def test_key_value_and_url_credentials() -> None:
    out = redact_text("password=hunter2hunter2 api_key: abc123 url=postgresql://user:pa55@db/x")
    assert "hunter2hunter2" not in out and "abc123" not in out and "pa55" not in out
    assert "postgresql://" in out


def test_private_key_block() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----"
    assert "AAAA" not in redact_text(pem)


def test_structures_redact_sensitive_keys() -> None:
    data = {"Authorization": "Bearer x", "nested": [{"session_token": "t"}, "ok"], "name": "ok"}
    out = redact_value(data)
    assert out["Authorization"] == REDACTED
    assert out["nested"][0]["session_token"] == REDACTED
    assert out["name"] == "ok"


def test_json_log_line_has_correlation_and_no_secrets() -> None:
    context.clear()
    context.bind(request_id="r1", run_id="run-9")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=supersecretvalue", None, None)
    record.password = "p@ss"
    line = json.loads(JsonFormatter().format(record))
    assert line["request_id"] == "r1" and line["run_id"] == "run-9"
    assert "supersecretvalue" not in json.dumps(line) and "p@ss" not in json.dumps(line)
    context.clear()


def test_redaction_keeps_json_valid() -> None:
    """Значение без кавычек не съедает закрывающую кавычку JSON-строки (отчёты gitleaks и др.)."""
    import json

    from aicd_platform.redaction import redact_text

    raw = json.dumps(
        {"Fingerprint": "abc:file.py:aws-access-token:3", "Secret": "AKIAABCDEFGHIJKLMNOP", "password": "x"}
    )
    redacted = redact_text(raw)
    parsed = json.loads(redacted)
    assert "AKIA" not in redacted and parsed["Fingerprint"].endswith("[REDACTED]")
