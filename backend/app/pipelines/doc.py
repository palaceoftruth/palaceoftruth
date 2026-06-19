import logging
from typing import Any

from app.pipelines.base import BasePipeline

logger = logging.getLogger(__name__)


class DocPipeline(BasePipeline):
    """Process pre-extracted document text through the standard pipeline.

    Supports .pdf, .docx, .xlsx, .md, .txt — text is extracted in the API
    background task before this pipeline is invoked via ARQ.
    """

    async def extract(self, extracted_text: str = "", doc_metadata: dict | None = None, **_kwargs) -> tuple[str, dict[str, Any]]:
        metadata = doc_metadata or {}
        if not extracted_text.strip():
            raise ValueError("No text provided for Doc pipeline")
        return extracted_text, metadata
