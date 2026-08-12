from __future__ import annotations

from app.config import settings
from app.database import _engine_configuration


def test_engine_configuration_uses_sslrootcert_from_database_url(
    monkeypatch, tmp_path
) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test certificate")
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql+asyncpg://palace:secret@example.test/palace"
        f"?sslmode=verify-full&sslrootcert={ca_file}",
    )
    monkeypatch.setattr(settings, "database_ssl_root_cert", "")
    captured: dict[str, str] = {}

    def create_context(*, cafile: str):
        captured["cafile"] = cafile
        return type("Context", (), {"check_hostname": False, "verify_mode": None})()

    monkeypatch.setattr("app.database.ssl.create_default_context", create_context)

    database_url, options = _engine_configuration()

    assert captured["cafile"] == str(ca_file)
    assert "sslrootcert" not in database_url
    assert options["connect_args"]["ssl"].check_hostname is True
