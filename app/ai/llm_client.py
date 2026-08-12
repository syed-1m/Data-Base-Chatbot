"""
app/ai/llm_client.py
=====================
Unified LLM client supporting Google Gemini and OpenAI.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass
class LLMResponse:
    content: str
    parsed_json: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    finish_reason: str = "stop"
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        self.total_tokens = self.input_tokens + self.output_tokens


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    fence_match2 = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match2:
        try:
            return json.loads(fence_match2.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break
    return None


from app.config import get_settings, settings


class GeminiClient:
    def __init__(self) -> None:
        import google.generativeai as genai
        cfg = get_settings()
        key = cfg.GEMINI_API_KEY.strip()
        if key:
            genai.configure(api_key=key)
        self._genai = genai

    async def generate(self, user_prompt: str, system_prompt: str | None = None) -> LLMResponse:
        cfg = get_settings()
        key = cfg.GEMINI_API_KEY.strip()
        if not key or key.startswith("your-"):
            raise ValueError("GEMINI_API_KEY in .env is not configured. Please set a valid Gemini API key in your .env file.")

        import google.generativeai as genai
        from google.generativeai.types import GenerationConfig

        genai.configure(api_key=key)

        model_kwargs = {
            "model_name": cfg.AI_MODEL,
            "generation_config": GenerationConfig(
                temperature=cfg.AI_TEMPERATURE,
                max_output_tokens=cfg.AI_MAX_OUTPUT_TOKENS,
            ),
        }
        full_user_prompt = user_prompt
        if system_prompt:
            if "1.0" in cfg.AI_MODEL or cfg.AI_MODEL == "gemini-pro":
                full_user_prompt = f"System Instructions:\n{system_prompt}\n\nUser Request:\n{user_prompt}"
            else:
                model_kwargs["system_instruction"] = system_prompt

        model = genai.GenerativeModel(**model_kwargs)

        start = time.perf_counter()
        response = await model.generate_content_async(full_user_prompt)
        latency_ms = (time.perf_counter() - start) * 1000

        content = response.text if response.text else ""
        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        parsed = _extract_json(content)

        return LLMResponse(
            content=content,
            parsed_json=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=cfg.AI_MODEL,
            finish_reason="stop",
            latency_ms=round(latency_ms, 1),
        )



class OpenAIClient:
    def __init__(self) -> None:
        import openai
        cfg = get_settings()
        self._client = openai.AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)

    async def generate(self, user_prompt: str, system_prompt: str | None = None) -> LLMResponse:
        cfg = get_settings()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        start = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=cfg.AI_MODEL,
            messages=messages,
            temperature=cfg.AI_TEMPERATURE,
            max_tokens=cfg.AI_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
        latency_ms = (time.perf_counter() - start) * 1000

        content = response.choices[0].message.content or ""
        usage = response.usage

        return LLMResponse(
            content=content,
            parsed_json=_extract_json(content),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=cfg.AI_MODEL,
            finish_reason=response.choices[0].finish_reason or "stop",
            latency_ms=round(latency_ms, 1),
        )


def get_llm_client() -> GeminiClient | OpenAIClient:
    cfg = get_settings()
    provider = cfg.AI_PROVIDER.lower()
    if provider == "gemini":
        return GeminiClient()
    elif provider == "openai":
        return OpenAIClient()
    else:
        raise ValueError(f"Unknown AI_PROVIDER: {provider}")
