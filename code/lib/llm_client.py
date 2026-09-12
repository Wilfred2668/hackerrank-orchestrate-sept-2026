"""
Thin wrapper around the Google Gemini (google-genai) SDK.

Responsibilities:
  - Load API key from environment / .env
  - Provide a single call() method for text and vision requests
  - Append one JSON line per call to code/logs/llm_calls.jsonl
  - Cache results keyed by (call_type, target_id, prompt_version)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CODE_DIR = Path(__file__).resolve().parent.parent
_LOGS_DIR = _CODE_DIR / "logs"
_CACHE_DIR = _CODE_DIR / "cache"
_LLM_LOG = _LOGS_DIR / "llm_calls.jsonl"

_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Load .env if present
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = _CODE_DIR.parent / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        os.environ.setdefault(key, value)


_load_dotenv()


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or api_key == "YOUR_KEY_HERE":
            raise RuntimeError(
                "GEMINI_API_KEY not set. "
                "Put it in .env or set the environment variable."
            )
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_call(
    provider: str,
    model: str,
    call_type: str,
    target_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "call_type": call_type,
        "target_id": target_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    with open(_LLM_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "v1"  # bump when extraction prompts change


def _cache_key(call_type: str, target_id: str) -> str:
    return f"{call_type}__{target_id}__{_PROMPT_VERSION}"


def _cache_path(call_type: str, target_id: str) -> Path:
    return _CACHE_DIR / f"{_cache_key(call_type, target_id)}.json"


def get_cached(call_type: str, target_id: str) -> Optional[str]:
    p = _cache_path(call_type, target_id)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("response_text")
    return None


def set_cache(call_type: str, target_id: str, response_text: str) -> None:
    p = _cache_path(call_type, target_id)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {
                "call_type": call_type,
                "target_id": target_id,
                "prompt_version": _PROMPT_VERSION,
                "response_text": response_text,
            },
            f,
            indent=2,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MODEL_FLASH = "gemini-3.1-flash-lite"


def _make_config(max_output_tokens: int = 4096) -> types.GenerateContentConfig:
    """Build config for Gemini generation."""
    return types.GenerateContentConfig(
        max_output_tokens=max_output_tokens,
    )


def _generate_with_retry(client: genai.Client, model: str, contents: Any, config: types.GenerateContentConfig, max_retries: int = 4) -> Any:
    """Execute generate_content with retry logic on 429 or 503 errors."""
    delay = 2.0
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            err_str = str(e)
            if ("429" in err_str or "503" in err_str or "RESOURCE_EXHAUSTED" in err_str) and attempt < max_retries - 1:
                # Check for retryDelay in error string
                retry_delay = delay
                import re
                m = re.search(r"retryDelay': '(\d+)s'", err_str)
                if m:
                    retry_delay = max(float(m.group(1)) + 1.0, retry_delay)
                print(f"[LLM Retry] {model} hit rate limit / busy. Waiting {retry_delay:.1f}s before retry {attempt+1}/{max_retries}...")
                time.sleep(retry_delay)
                delay *= 2
            else:
                raise


def call_text(
    prompt: str,
    *,
    call_type: str,
    target_id: str,
    model: Optional[str] = None,
    use_cache: bool = True,
) -> str:
    """Send a text-only prompt. Returns the model's text response."""
    actual_model = model or MODEL_FLASH
    if use_cache:
        cached = get_cached(call_type, target_id)
        if cached is not None:
            return cached

    client = _get_client()
    response = _generate_with_retry(
        client,
        actual_model,
        prompt,
        _make_config(),
    )
    text = response.text or ""
    input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
    output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
    thoughts_tokens = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

    _log_call("google", actual_model, call_type, target_id, input_tokens, output_tokens + thoughts_tokens)
    if use_cache:
        set_cache(call_type, target_id, text)
    return text


def call_vision(
    prompt: str,
    image_path: str,
    *,
    call_type: str,
    target_id: str,
    model: Optional[str] = None,
    use_cache: bool = True,
) -> str:
    """Send a vision prompt with an image file. Returns the model's text response."""
    actual_model = model or MODEL_FLASH
    if use_cache:
        cached = get_cached(call_type, target_id)
        if cached is not None:
            return cached

    client = _get_client()

    # Read the image and upload it
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Use inline data for the image
    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/png",
    )

    response = _generate_with_retry(
        client,
        actual_model,
        [image_part, prompt],
        _make_config(),
    )
    text = response.text or ""
    input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
    output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
    thoughts_tokens = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

    _log_call("google", actual_model, call_type, target_id, input_tokens, output_tokens + thoughts_tokens)
    if use_cache:
        set_cache(call_type, target_id, text)
    return text

