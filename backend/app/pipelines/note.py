from typing import Any

from app.pipelines.base import BasePipeline


class NotePipeline(BasePipeline):
    """Plain text note — content is provided directly, no extraction needed."""

    async def extract(self, title: str, content: str, tags: list[str] | None = None, **_kwargs) -> tuple[str, dict[str, Any]]:
        metadata: dict[str, Any] = {
            "title": title,
            "word_count": len(content.split()),
            "char_count": len(content),
        }
        if tags:
            metadata["manual_tags"] = tags
        return content, metadata
