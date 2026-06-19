import logging
from typing import Any

from app.pipelines.base import BasePipeline

logger = logging.getLogger(__name__)


class PDFPipeline(BasePipeline):
    """Process pre-extracted PDF text through the standard pipeline."""

    async def extract(self, extracted_text: str = "", pdf_metadata: dict | None = None, **_kwargs) -> tuple[str, dict[str, Any]]:
        metadata = pdf_metadata or {}
        if not extracted_text.strip():
            raise ValueError("No text provided for PDF pipeline")
        return extracted_text, metadata
