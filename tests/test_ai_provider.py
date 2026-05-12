import sys
import types

import ai_provider


def test_get_ai_settings_prefers_deepseek_primary(monkeypatch):
    monkeypatch.setattr(ai_provider, "load_dotenv_if_present", lambda: None)
    for key in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fallback-key")

    settings = ai_provider.get_ai_settings()

    assert settings["provider"] == "deepseek"
    assert settings["api_key"] == "deepseek-key"
    assert settings["model"] == "deepseek-v4-flash"
    assert settings["base_url"] == "https://api.deepseek.com"


def test_get_ai_fallback_settings_uses_openrouter_free_models(monkeypatch):
    monkeypatch.setattr(ai_provider, "load_dotenv_if_present", lambda: None)
    for key in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_BACKUP_MODEL",
        "AI_BACKUP_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("OPENROUTER_API_KEY", "fallback-key")

    fallbacks = ai_provider.get_ai_fallback_settings({"provider": "deepseek"})

    assert [f["provider"] for f in fallbacks] == ["openrouter", "openrouter"]
    assert [f["model"] for f in fallbacks] == [
        "openai/gpt-oss-120b:free",
        "z-ai/glm-4.5-air:free",
    ]
    assert all(f["api_key"] == "fallback-key" for f in fallbacks)


def test_deepseek_chat_completion_disables_thinking_and_requests_json(monkeypatch):
    calls = []

    monkeypatch.setattr(ai_provider, "get_ai_settings", lambda: {
        "provider": "deepseek",
        "api_key": "deepseek-key",
        "base_url": "https://api.deepseek.com",
        "timeout": 30,
    })

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": '{"schedule":[]}'}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            }

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            calls.append((url, headers, json))
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    fake_httpx = types.SimpleNamespace(Client=FakeClient, Timeout=FakeTimeout)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    response = ai_provider.create_chat_completion(
        client=None,
        system_prompt="Return JSON only.",
        user_prompt='{"slots":[]}',
        model="deepseek-v4-flash",
        max_tokens=100,
    )

    assert calls[0][0] == "https://api.deepseek.com/chat/completions"
    assert calls[0][2]["thinking"] == {"type": "disabled"}
    assert calls[0][2]["response_format"] == {"type": "json_object"}
    assert response.choices[0].message.content == '{"schedule":[]}'


def test_create_chat_completion_retries_rate_limit(monkeypatch):
    calls = []
    sleeps = []

    monkeypatch.setattr(ai_provider, "get_ai_settings", lambda: {
        "api_key": "test-key",
        "base_url": "https://openrouter.ai/api/v1",
    })
    monkeypatch.setattr(ai_provider.time, "sleep", lambda seconds: sleeps.append(seconds))

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {"retry-after": "0.2"}
            self.text = "rate limited"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise FakeHTTPStatusError("429", response=self)

        def json(self):
            return {
                "choices": [{"message": {"content": '{"schedule":[]}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }

    class FakeHTTPStatusError(Exception):
        def __init__(self, message, response):
            super().__init__(message)
            self.response = response

    class FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            calls.append((url, json))
            return FakeResponse(429 if len(calls) == 1 else 200)

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    fake_httpx = types.SimpleNamespace(
        Client=FakeClient,
        Timeout=FakeTimeout,
        HTTPStatusError=FakeHTTPStatusError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    response = ai_provider.create_chat_completion(
        client=None,
        system_prompt="system",
        user_prompt="user",
        model="test-model",
        max_tokens=100,
    )

    assert len(calls) == 2
    assert sleeps == [0.2]
    assert response.choices[0].message.content == '{"schedule":[]}'
