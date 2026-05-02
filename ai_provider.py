import os
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

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


def get_ai_settings() -> Optional[dict]:
    load_dotenv_if_present()
    api_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    placeholder_values = {"your_openrouter_api_key_here", "your_openai_api_key_here", "changeme", "placeholder"}
    if not api_key or api_key.strip().lower() in placeholder_values:
        return None

    return {
        "api_key": api_key,
        "model": os.environ.get("OPENROUTER_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL,
        "base_url": os.environ.get("OPENROUTER_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
    }


def create_ai_client() -> Tuple[Optional[OpenAI], Optional[dict]]:
    if not OPENAI_AVAILABLE:
        return None, None

    settings = get_ai_settings()
    if not settings:
        return None, None

    client = OpenAI(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        default_headers={
            "HTTP-Referer": "https://studio-scheduler.local",
            "X-Title": "Studio Scheduler",
        },
    )
    return client, settings


def create_chat_completion(client: OpenAI, system_prompt: str, user_prompt: str, model: str, max_tokens: int):
    return client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
