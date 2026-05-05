from pathlib import Path

from app import supabase_settings as flask_supabase_settings
from serve import supabase_settings as stdlib_supabase_settings


ROOT = Path(__file__).resolve().parents[1]


def test_supabase_schema_does_not_grant_anon_access():
    source = (ROOT / "supabase" / "schema.sql").read_text().lower()

    assert "to anon" not in source
    assert "anon," not in source


def test_supabase_settings_require_service_role_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_KEY", "generic-key")

    assert stdlib_supabase_settings() == ("https://example.supabase.co", "")
    assert flask_supabase_settings() == ("https://example.supabase.co", "")


def test_supabase_settings_accept_rest_endpoint_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/rest/v1")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")

    assert stdlib_supabase_settings() == ("https://example.supabase.co", "service-key")
    assert flask_supabase_settings() == ("https://example.supabase.co", "service-key")


def test_schedule_supabase_save_failure_does_not_raise(monkeypatch):
    import app

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(app, "supabase_upsert", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad path")))

    result = app._save_schedule_to_supabase({"locations": {}})

    assert result["saved"] is False
    assert "bad path" in result["error"]
