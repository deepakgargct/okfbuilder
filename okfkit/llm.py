"""Tiny, dependency-free LLM clients that return parsed JSON.

Model names are user-editable in the sidebar because providers retire
model IDs often.
"""
from __future__ import annotations

import json
import re

import requests

DEFAULT_MODELS = {
    "Anthropic": "claude-sonnet-5-5",
    "Google Gemini": "gemini-2.5-flash",
    "OpenAI": "gpt-4.1-mini",
}


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise LLMError("Model did not return JSON")


def complete_json(provider: str, model: str, api_key: str, system: str, user: str,
                  max_tokens: int = 4000, timeout: int = 120) -> dict:
    if not api_key:
        raise LLMError("Missing API key")
    try:
        if provider == "Anthropic":
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=timeout)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        elif provider == "Google Gemini":
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"responseMimeType": "application/json",
                                           "maxOutputTokens": max_tokens}},
                timeout=timeout)
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        elif provider == "OpenAI":
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        else:
            raise LLMError(f"Unknown provider {provider}")
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise LLMError(f"{provider} HTTP {e.response.status_code if e.response is not None else '?'}: {body}") from e
    except (requests.RequestException, KeyError, IndexError) as e:
        raise LLMError(f"{provider} call failed: {e}") from e
    return _extract_json(text)
