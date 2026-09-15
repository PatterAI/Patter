"""Atlas Cloud LLM provider for Patter's pipeline mode.

Atlas Cloud's Chat Completions API is OpenAI-compatible, so this provider is a
thin wrapper around :class:`getpatter.services.llm_loop.OpenAILLMProvider`
that points at ``https://api.atlascloud.ai/v1``. All sampling kwargs
(``response_format``, ``parallel_tool_calls``, ``tool_choice``, ``seed``,
``top_p``, ``frequency_penalty``, ``presence_penalty``, ``stop``,
``temperature``, ``max_tokens``) are inherited from the parent and
forwarded to ``chat.completions.create`` automatically.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import ClassVar

from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["AtlasCloudLLMProvider", "AtlasCloudModel"]

logger = logging.getLogger("getpatter.providers.atlascloud_llm")


class AtlasCloudModel(StrEnum):
    """Known Atlas Cloud Chat Completions models. The gateway serves many more;
    ``GET /v1/models`` lists what is currently routable."""

    GPT_4_1_MINI = "openai/gpt-4.1-mini"
    GPT_5_4_MINI = "openai/gpt-5.4-mini"
    DEEPSEEK_V3_2 = "deepseek-ai/deepseek-v3.2"
    QWEN_3_235B_INSTRUCT = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    QWEN_3_5_35B_A3B = "qwen/qwen3.5-35b-a3b"
    GLM_4_6 = "zai-org/GLM-4.6"
    KIMI_K2_5 = "moonshotai/kimi-k2.5"
    MINIMAX_M2_5 = "minimaxai/minimax-m2.5"


_ATLASCLOUD_BASE_URL = "https://api.atlascloud.ai/v1"
_DEFAULT_MODEL = AtlasCloudModel.GPT_4_1_MINI.value


class AtlasCloudLLMProvider(OpenAILLMProvider):
    """LLM provider backed by Atlas Cloud's OpenAI-compatible Chat Completions API.

    Streams in the same ``{"type": "text" | "tool_call" | "done"}`` chunk
    format as :class:`OpenAILLMProvider`. All OpenAI-spec sampling kwargs
    accepted by the parent (``response_format``, ``parallel_tool_calls``,
    ``tool_choice``, ``seed``, ``top_p``, ``frequency_penalty``,
    ``presence_penalty``, ``stop``, ``temperature``, ``max_tokens``) are
    forwarded transparently — see :class:`OpenAILLMProvider` for details.

    Args:
        api_key: Atlas Cloud API key. If omitted, ``ATLASCLOUD_API_KEY`` is read from
            the environment.
        model: Atlas Cloud model ID. Defaults to ``openai/gpt-4.1-mini``.
        base_url: Optional Atlas Cloud base URL override.
        **kwargs: Sampling kwargs forwarded to
            :class:`OpenAILLMProvider`.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "atlascloud"

    def __init__(
        self,
        api_key: str | None = None,
        model: AtlasCloudModel | str = _DEFAULT_MODEL,
        base_url: str = _ATLASCLOUD_BASE_URL,
        **kwargs,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "The 'openai' package is required for AtlasCloudLLMProvider. "
                "Install it with: pip install 'getpatter[atlascloud]'"
            ) from e

        resolved_key = api_key or os.environ.get("ATLASCLOUD_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Atlas Cloud API key is required, either as the 'api_key' "
                "argument or via the ATLASCLOUD_API_KEY environment variable."
            )

        # Initialise parent state (model, sampling kwargs, _user_agent)
        # without using its OpenAI-pointed client. We swap in an Atlas-pointed
        # client below using the same User-Agent the parent computed.
        super().__init__(api_key=resolved_key, model=model, **kwargs)
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            default_headers={"User-Agent": self._user_agent},
        )

    def _record_completion_cost(
        self, *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Stamp ``patter.cost.llm_*_tokens`` with the Atlas Cloud provider tag."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.llm_input_tokens": prompt_tokens,
                    "patter.cost.llm_output_tokens": completion_tokens,
                    "patter.llm.provider": "atlascloud",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_completion_cost failed", exc_info=True)
