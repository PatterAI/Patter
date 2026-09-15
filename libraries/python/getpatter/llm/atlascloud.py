"""Atlas Cloud LLM for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.atlascloud_llm import AtlasCloudLLMProvider as _AtlasCloudLLM

__all__ = ["LLM"]


class LLM(_AtlasCloudLLM):
    """Atlas Cloud LLM provider (OpenAI-compatible Chat Completions).

    Example::

        from getpatter.llm import atlascloud

        llm = atlascloud.LLM()                        # reads ATLASCLOUD_API_KEY
        llm = atlascloud.LLM(api_key="apikey-...", model="deepseek-ai/deepseek-v3.2")
    """

    provider_key: ClassVar[str] = "atlascloud"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "openai/gpt-4.1-mini",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("ATLASCLOUD_API_KEY")
        if not key:
            raise ValueError(
                "Atlas Cloud LLM requires an api_key. Pass "
                "api_key='apikey-...' or set ATLASCLOUD_API_KEY in the "
                "environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
