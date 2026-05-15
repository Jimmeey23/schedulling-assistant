import os
import time
import types
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_BACKUP_MODEL = "z-ai/glm-4.5-air:free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 30
PLACEHOLDER_VALUES = {
    "your_openrouter_api_key_here",
    "your_openai_api_key_here",
    "your_deepseek_api_key_here",
    "changeme",
    "placeholder",
}

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    OPENAI_AVAILABLE = False


def load_dotenv_if_present() -> None:
    if not ENV_PATH.exists():
        return
    try:
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ and value:
                os.environ[key] = value
    except Exception:
        # Silent on purpose; env loading is best-effort only.
        return


def _clean_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key or key.lower() in PLACEHOLDER_VALUES:
        return ""
    return key


def _timeout_seconds() -> float:
    return float(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)


def _settings(provider: str, api_key: str, model: str, base_url: str, backup_model: str = "") -> dict:
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "backup_model": backup_model,
        "base_url": str(base_url or "").rstrip("/"),
        "timeout": _timeout_seconds(),
    }


def get_ai_settings() -> Optional[dict]:
    load_dotenv_if_present()

    deepseek_key = _clean_key(os.environ.get("DEEPSEEK_API_KEY"))
    if deepseek_key:
        return _settings(
            provider="deepseek",
            api_key=deepseek_key,
            model=os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL,
            backup_model=os.environ.get("DEEPSEEK_BACKUP_MODEL") or "",
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL,
        )

    openrouter_key = _clean_key(os.environ.get("OPENROUTER_API_KEY"))
    if openrouter_key:
        return _settings(
            provider="openrouter",
            api_key=openrouter_key,
            model=os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL,
            backup_model=os.environ.get("AI_BACKUP_MODEL") or os.environ.get("OPENROUTER_BACKUP_MODEL") or DEFAULT_BACKUP_MODEL,
            base_url=os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL,
        )

    openai_key = _clean_key(os.environ.get("OPENAI_API_KEY"))
    if openai_key:
        return _settings(
            provider="openai",
            api_key=openai_key,
            model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
            backup_model=os.environ.get("AI_BACKUP_MODEL") or "",
            base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL,
        )

    return None


def get_ai_fallback_settings(primary_settings: Optional[dict] = None) -> list[dict]:
    load_dotenv_if_present()
    primary_provider = str((primary_settings or {}).get("provider") or "").lower()
    primary_model = str((primary_settings or {}).get("model") or "")
    fallbacks: list[dict] = []

    openrouter_key = _clean_key(os.environ.get("OPENROUTER_API_KEY"))
    if openrouter_key and primary_provider != "openrouter":
        seen_models = {primary_model}
        for model in (
            os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL,
            os.environ.get("AI_BACKUP_MODEL") or os.environ.get("OPENROUTER_BACKUP_MODEL") or DEFAULT_BACKUP_MODEL,
        ):
            model = str(model or "").strip()
            if not model or model in seen_models:
                continue
            seen_models.add(model)
            fallbacks.append(_settings(
                provider="openrouter",
                api_key=openrouter_key,
                model=model,
                backup_model="",
                base_url=os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL,
            ))

    return fallbacks


def create_ai_client() -> Tuple[Optional[OpenAI], Optional[dict]]:
    if not OPENAI_AVAILABLE:
        return None, None

    settings = get_ai_settings()
    if not settings:
        return None, None

    client = OpenAI(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        timeout=settings["timeout"],
        max_retries=0,
        default_headers={
            "HTTP-Referer": "https://studio-scheduler.local",
            "X-Title": "Studio Scheduler",
        },
    )
    return client, settings


def create_chat_completion(
    client: OpenAI,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    settings_override: Optional[dict] = None,
):
    try:
        import httpx
    except ImportError:
        httpx = None

    settings = settings_override or get_ai_settings()
    timeout = float((settings or {}).get("timeout") or os.environ.get("AI_REQUEST_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Use direct HTTP for schedule generation. The OpenAI SDK can keep large
    # OpenRouter requests alive longer than expected; explicit httpx timeouts
    # make Generate with AI fail or fall back instead of hanging indefinitely.
    if httpx is not None:
        if settings:
            base_url = str(settings.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
            url = f"{base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {settings['api_key']}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://studio-scheduler.local",
                "X-Title": "Studio Scheduler",
            }
            payload = {
                "model": model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if str(settings.get("provider") or "").lower() == "deepseek":
                payload["thinking"] = {"type": "disabled"}
                payload["response_format"] = {"type": "json_object"}
            http_timeout = httpx.Timeout(timeout, connect=min(10.0, timeout), read=timeout, write=min(20.0, timeout), pool=10.0)
            max_attempts = int(os.environ.get("AI_RATE_LIMIT_RETRIES") or 3)
            with httpx.Client(timeout=http_timeout) as http:
                for attempt in range(max(1, max_attempts)):
                    resp = http.post(url, headers=headers, json=payload)
                    try:
                        resp.raise_for_status()
                    except Exception as exc:
                        status_code = getattr(getattr(exc, "response", None), "status_code", None)
                        if status_code == 429 and attempt < max_attempts - 1:
                            retry_after = getattr(resp, "headers", {}).get("retry-after")
                            try:
                                sleep_seconds = float(retry_after)
                            except (TypeError, ValueError):
                                sleep_seconds = min(8.0, 1.5 * (attempt + 1))
                            time.sleep(max(0.1, sleep_seconds))
                            continue
                        raise
                    data = resp.json()
                    break
            choices = []
            for choice in data.get("choices") or []:
                msg = choice.get("message") or {}
                choices.append(types.SimpleNamespace(message=types.SimpleNamespace(content=msg.get("content") or "")))
            usage_data = data.get("usage") or {}
            usage = types.SimpleNamespace(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )
            return types.SimpleNamespace(choices=choices, usage=usage)

    return client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=max_tokens,
        timeout=timeout,
        messages=messages,
    )


def call_ai(
    prompt: str,
    max_tokens: int = 1024,
    system_prompt: str = "Return JSON only.",
    api_key: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> str:
    """Return raw assistant text using either explicit runtime settings or env config."""
    if not OPENAI_AVAILABLE:
        raise RuntimeError("AI client not available. Install the openai package.")

    explicit_key = _clean_key(api_key)
    if explicit_key:
        provider_name = str(provider or "deepseek").strip().lower()
        if provider_name == "openai":
            default_model = "gpt-4o-mini"
            default_base_url = DEFAULT_OPENAI_BASE_URL
        elif provider_name == "openrouter":
            default_model = DEFAULT_MODEL
            default_base_url = DEFAULT_BASE_URL
        else:
            provider_name = "deepseek"
            default_model = DEFAULT_DEEPSEEK_MODEL
            default_base_url = DEFAULT_DEEPSEEK_BASE_URL
        settings = _settings(
            provider=provider_name,
            api_key=explicit_key,
            model=str(model or default_model).strip(),
            base_url=str(base_url or default_base_url).strip(),
        )
        client = OpenAI(
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            timeout=settings["timeout"],
            max_retries=0,
            default_headers={
                "HTTP-Referer": "https://studio-scheduler.local",
                "X-Title": "Studio Scheduler",
            },
        )
    else:
        client, settings = create_ai_client()
        if not client or not settings:
            raise RuntimeError("All AI providers failed - configure at least one API key.")

    response = create_chat_completion(
        client=client,
        system_prompt=system_prompt,
        user_prompt=prompt,
        model=settings["model"],
        max_tokens=max_tokens,
        settings_override=settings,
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("AI provider returned an empty response.")
    return content

# ---------------------------------------------------------------------------
# Fallback chat implementation – tries primary model first, then GLM‑4.5‑air, then Owl‑Alpha
# ---------------------------------------------------------------------------
import json

GLM_MODEL = "z-ai/glm-4.5-air:free"
OWL_MODEL = "openrouter/owl-alpha"

def _make_client(key: str):
    """Create an OpenAI-compatible client for a given API key."""
    return OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")

def _call_model(client: OpenAI, system_prompt: str, user_prompt: str, model: str, max_tokens: int = 1024):
    """Low‑level call that returns the assistant reply or raises.
    This mirrors the signature used elsewhere in the app.
    """
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    return resp.choices[0].message.content

def get_chat_reply(system_prompt: str, user_prompt: str) -> str:
    """Try the primary configured AI, then GLM‑4.5‑air, then Owl‑Alpha.
    Returns the first successful reply. Raises RuntimeError if none work.
    """
    # Primary – use whatever key/model the app already configured via ai_provider.get_ai_settings()
    primary_settings = get_ai_settings()
    if primary_settings:
        try:
            client = OpenAI(
                api_key=primary_settings["api_key"],
                base_url=primary_settings.get("base_url") or DEFAULT_BASE_URL,
                default_headers={
                    "HTTP-Referer": "https://studio-scheduler.local",
                    "X-Title": "Studio Scheduler",
                },
            )
            return _call_model(client, system_prompt, user_prompt, primary_settings["model"])
        except Exception as e:
            print(f"[AI] Primary model failed ({primary_settings['model']}): {e}")
    # GLM fallback – expects GLM_API_KEY in env
    glm_key = os.getenv("GLM_API_KEY")
    if glm_key:
        try:
            client = _make_client(glm_key)
            return _call_model(client, system_prompt, user_prompt, GLM_MODEL)
        except Exception as e:
            print(f"[AI] GLM fallback failed: {e}")
    # Owl fallback – expects OWL_API_KEY in env
    owl_key = os.getenv("OWL_API_KEY")
    if owl_key:
        try:
            client = _make_client(owl_key)
            return _call_model(client, system_prompt, user_prompt, OWL_MODEL)
        except Exception as e:
            print(f"[AI] Owl fallback failed: {e}")
    raise RuntimeError("All AI providers failed – configure at least one API key.")
