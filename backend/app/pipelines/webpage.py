import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import trafilatura
from playwright.async_api import async_playwright

from app.pipelines.base import BasePipeline
from app.pipelines.social import (
    SocialCaptureError,
    capture_social_post,
    detect_social_post_platform,
)
from app.pipelines.youtube import MediaTranscriptionLimitError, MediaPipeline

_WORDS_PER_MINUTE = 200

logger = logging.getLogger(__name__)

# Chromium flags required for headless operation inside Docker/Kubernetes
_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",  # avoids /dev/shm exhaustion in k8s
    "--disable-gpu",
]


class WebpagePipeline(BasePipeline):
    """Scrape article text from a URL.

    Strategy:
    1. trafilatura (fast, no browser) — works for most sites
    2. Playwright headless Chromium — fallback for JS-rendered / bot-protected sites
       - Navigates and waits for network idle
       - Asks the LLM to identify and dismiss any overlays (cookie banners, age gates)
       - Extracts the rendered HTML and passes it back through trafilatura
    """

    async def extract(self, url: str, job_id: str = "unknown", **_kwargs) -> tuple[str, dict[str, Any]]:
        social_platform = detect_social_post_platform(url)

        # Fast path: trafilatura (synchronous — run in executor)
        loop = asyncio.get_event_loop()
        _, article_text, metadata = await loop.run_in_executor(None, self._scrape, url)

        if article_text:
            article_text, metadata = await self._append_social_video_transcript(
                article_text,
                metadata,
                job_id=job_id,
            )
            return article_text, metadata

        if social_platform:
            detail = metadata.get("social_capture_error") or "no readable post text was returned"
            raise ValueError(
                f"Could not capture public {social_platform} post from server-side metadata without JavaScript: {detail}"
            )

        # Slow path: headless browser
        logger.info("trafilatura got nothing for %s — trying Playwright", url)
        article_text, metadata = await self._scrape_with_browser(url)
        if not article_text:
            raise ValueError(f"Could not extract text from {url} (tried trafilatura and Playwright)")
        return article_text, metadata

    async def _append_social_video_transcript(
        self,
        article_text: str,
        metadata: dict[str, Any],
        *,
        job_id: str,
    ) -> tuple[str, dict[str, Any]]:
        video_url = metadata.get("primary_video_url")
        if not isinstance(video_url, str) or not video_url:
            return article_text, metadata

        try:
            transcript, media_metadata = await MediaPipeline(self.db, self.embedder, self.llm).extract(
                video_url,
                job_id=job_id,
            )
        except MediaTranscriptionLimitError as exc:
            logger.info("social video transcription skipped for %s: %s", video_url, exc)
            return (
                article_text,
                {
                    **metadata,
                    "social_video_transcription_error": str(exc)[:500],
                },
            )
        except Exception as exc:
            logger.warning("social video transcription failed for %s: %s", video_url, exc)
            return (
                article_text,
                {
                    **metadata,
                    "social_video_transcription_error": str(exc)[:500],
                },
            )

        merged_metadata = {
            **metadata,
            "social_video_transcribed": True,
            "social_video_transcript_url": video_url,
            "social_video_metadata": media_metadata,
        }
        combined_text = (
            f"{article_text.strip()}\n\n---\n\n"
            f"Attached Video Transcript:\n{transcript.strip()}"
        ).strip()
        return combined_text, merged_metadata

    @staticmethod
    def _scrape(url: str) -> tuple[str | None, str | None, dict[str, Any]]:
        """Synchronous trafilatura scrape. Used directly by FeedPipeline too."""
        metadata: dict[str, Any] = {}

        if detect_social_post_platform(url):
            try:
                capture = capture_social_post(url)
            except SocialCaptureError as exc:
                metadata["social_capture_error"] = str(exc)[:500]
            except Exception as exc:
                logger.warning("social post capture failed for %s: %s", url, exc)
                metadata["social_capture_error"] = str(exc)[:500]
            else:
                if capture:
                    return capture.html, capture.text, capture.metadata

        html = trafilatura.fetch_url(url)
        if not html:
            return None, None, metadata
        meta = trafilatura.extract_metadata(html)
        article = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            output_format="txt",
        )
        if meta:
            if meta.title:
                metadata["title"] = meta.title
            if meta.author:
                metadata["author"] = meta.author
            if meta.date:
                date_str = str(meta.date)
                metadata["date"] = date_str
                metadata["published_at"] = date_str
            if meta.sitename:
                metadata["sitename"] = meta.sitename
        # Domain and estimated read time (R14)
        try:
            metadata["domain"] = urlparse(url).netloc
        except Exception:
            pass
        if article:
            word_count = len(article.split())
            metadata["estimated_read_time_minutes"] = max(1, round(word_count / _WORDS_PER_MINUTE))
        return html, article, metadata

    async def _scrape_with_browser(self, url: str) -> tuple[str | None, dict[str, Any]]:
        """Navigate with headless Chromium, let the LLM dismiss any overlays, extract text."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
            try:
                page = await browser.new_page()
                await page.set_extra_http_headers({
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                })

                await page.goto(url, wait_until="networkidle", timeout=30_000)

                # Ask the LLM whether anything needs to be clicked first
                visible_text = await page.inner_text("body")
                actions = await self.llm.get_browser_actions(visible_text, url)

                if actions:
                    logger.info("Browser actions for %s: %s", url, actions)
                    for action in actions:
                        try:
                            await page.get_by_text(action["text"], exact=False).first.click(timeout=3_000)
                            await page.wait_for_load_state("networkidle", timeout=5_000)
                        except Exception as exc:
                            logger.debug("Browser action failed (%s): %s", action, exc)

                # Pull the fully-rendered HTML and run it through trafilatura
                html = await page.content()
            finally:
                await browser.close()

        meta = trafilatura.extract_metadata(html)
        article = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            output_format="txt",
        )
        metadata: dict[str, Any] = {"scraped_with": "playwright"}
        if meta:
            if meta.title:
                metadata["title"] = meta.title
            if meta.author:
                metadata["author"] = meta.author
            if meta.date:
                date_str = str(meta.date)
                metadata["date"] = date_str
                metadata["published_at"] = date_str
            if meta.sitename:
                metadata["sitename"] = meta.sitename
        try:
            metadata["domain"] = urlparse(url).netloc
        except Exception:
            pass
        if article:
            word_count = len(article.split())
            metadata["estimated_read_time_minutes"] = max(1, round(word_count / _WORDS_PER_MINUTE))
        return article, metadata
