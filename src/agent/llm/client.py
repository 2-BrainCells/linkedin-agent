from __future__ import annotations

import json
import re
from typing import Any

import ollama
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.config import OllamaSettings, load_settings


class LLMError(RuntimeError):
    pass


def _client(settings: OllamaSettings | None = None) -> ollama.Client:
    settings = settings or load_settings().ollama
    return ollama.Client(host=settings.host, timeout=settings.request_timeout_seconds)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def chat(model: str, messages: list[dict[str, str]], *, json_mode: bool = False,
         temperature: float = 0.2) -> str:
    """Send a chat request to Ollama; return the assistant string."""
    settings = load_settings().ollama
    client = _client(settings)
    options: dict[str, Any] = {"temperature": temperature}
    fmt = "json" if json_mode else None
    try:
        resp = client.chat(
            model=model,
            messages=messages,
            options=options,
            format=fmt,
        )
    except Exception as e:
        raise LLMError(f"Ollama chat failed for model={model}: {e}") from e
    content = resp.get("message", {}).get("content", "").strip()
    if not content:
        raise LLMError(f"Empty response from model={model}")
    return content


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(raw: str) -> dict:
    """Tolerant JSON extraction — strips code fences, finds first {...} blob."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise LLMError(f"Could not extract JSON from response: {raw[:200]!r}")
        return json.loads(match.group(0))


def is_model_available(model: str) -> bool:
    try:
        client = _client()
        models = {m["name"] for m in client.list().get("models", [])}
        # Ollama returns names like "llama3.1:8b"; allow either exact or stem match.
        return model in models or any(m.split(":")[0] == model.split(":")[0] for m in models)
    except Exception as e:
        logger.warning(f"Could not list Ollama models: {e}")
        return False


__all__ = ["chat", "parse_json", "is_model_available", "LLMError"]
