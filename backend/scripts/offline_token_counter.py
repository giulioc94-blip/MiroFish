"""
Approximate token counter for models whose tokenizer is not available locally.

camel's default OpenAITokenCounter downloads tiktoken encodings on first use.
That download is impossible in sandboxed or offline environments, and it is
meaningless for non-OpenAI models served through an OpenAI-compatible endpoint
(for example Claude). This counter estimates token counts from character
length instead, which is sufficient for the context-budget checks the
simulation performs.
"""

from typing import List

from camel.utils.token_counting import BaseTokenCounter

# Rough average for mixed Italian/English prose and JSON.
_CHARS_PER_TOKEN = 3.5
# Per-message overhead used by OpenAI-style chat formats.
_TOKENS_PER_MESSAGE = 4


class ApproxTokenCounter(BaseTokenCounter):
    """Character-based token estimate that never touches the network."""

    def count_tokens_from_messages(self, messages: List[dict]) -> int:
        total = 0
        for message in messages:
            total += _TOKENS_PER_MESSAGE
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                )
            total += len(self.encode(str(content or "")))
        return total

    def encode(self, text: str) -> List[int]:
        n = int(len(text) / _CHARS_PER_TOKEN) + (1 if text else 0)
        return list(range(n))

    def decode(self, token_ids: List[int]) -> str:
        # Token IDs carry no text in this estimator; decoding is not meaningful.
        return ""


def needs_offline_counter(model_name: str) -> bool:
    """True for model families that tiktoken cannot tokenize."""
    return (model_name or "").strip().lower().startswith("claude")
