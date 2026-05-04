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
