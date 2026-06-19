"""Server-side capture helpers for social post URLs.

The normal webpage pipeline is article-oriented. X and Facebook post pages are
usually application shells, so article extraction often fails unless a browser
can run their JavaScript. These helpers use provider metadata endpoints and
static page metadata first so capture still works in no-JavaScript contexts.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 12.0
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_X_STATUS_RE = re.compile(r"^/[^/]+/status(?:es)?/\d+/?", re.IGNORECASE)
_X_ARTICLE_SUSPICIOUSLY_SHORT_CHARS = 240
_FACEBOOK_POST_PATH_RE = re.compile(
    r"("
    r"/posts/|"
    r"/permalink\.php|"
    r"/story\.php|"
    r"/photo\.php|"
    r"/groups/[^/]+/posts/|"
    r"/share/(?:p|v|r)/|"
    r"/watch/|"
    r"/reel/"
    r")",
    re.IGNORECASE,
)

_LOW_VALUE_TEXT_MARKERS = (
    "log in to facebook",
    "sign up for facebook",
    "welcome to facebook",
    "connect with friends",
    "see posts, photos and more on facebook",
    "x. it's what's happening",
    "twitter. it's what's happening",
)

_X_VIDEO_URL_RE = re.compile(
    r"^https?://[^/?#]*(?:video\.twimg\.com|video\.twitter\.com|x\.com|twitter\.com)/.+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SocialPostCapture:
    text: str
    html: str | None
    metadata: dict[str, Any]


class SocialCaptureError(RuntimeError):
    """Raised when a recognized social post URL has no server-side text."""


@dataclass(frozen=True)
class _XArticleCapture:
    text: str
    block_text: str
    block_count: int
    used_preview_only: bool
    warning: str | None = None


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"blockquote", "br", "div", "li", "p"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"blockquote", "div", "li", "p"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return _normalize_text(" ".join(self.parts))


class _MetadataHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        key = attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop")
        content = attr_map.get("content")
        if key and content:
            self.meta[key.lower()] = _normalize_text(content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self._title_parts.append(data)

    def metadata(self) -> dict[str, str]:
        metadata = dict(self.meta)
        title = _normalize_text(" ".join(self._title_parts))
        if title:
            metadata["html_title"] = title
        return metadata


def detect_social_post_platform(url: str) -> str | None:
    """Return the social provider for recognized single-post URLs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = _normalized_host(parsed.netloc)
    if host in {"x.com", "twitter.com"} and _X_STATUS_RE.match(parsed.path):
        return "x"

    if host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch":
        if host == "fb.watch" or _FACEBOOK_POST_PATH_RE.search(parsed.path):
            return "facebook"

    return None


def capture_social_post(url: str, client: httpx.Client | None = None) -> SocialPostCapture | None:
    """Capture readable social post text without rendering browser JavaScript."""
    platform = detect_social_post_platform(url)
    if not platform:
        return None

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=_HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    errors: list[str] = []
    try:
        if platform == "x":
            capture = _capture_x_fxtwitter(client, url, errors)
            if capture:
                return capture

            capture = _capture_x_oembed(client, url, errors)
            if capture:
                return capture

        if platform == "facebook":
            capture = _capture_facebook_oembed(client, url, errors)
            if capture:
                return capture

        capture = _capture_static_metadata(client, url, platform, errors)
        if capture:
            return capture
    finally:
        if owns_client:
            client.close()

    detail = "; ".join(errors) if errors else "provider metadata did not include readable post text"
    raise SocialCaptureError(detail)


def _capture_x_fxtwitter(
    client: httpx.Client,
    url: str,
    errors: list[str],
) -> SocialPostCapture | None:
    status_id = _x_status_id(url)
    if not status_id:
        errors.append("X URL does not include a status id")
        return None

    response = client.get(
        f"https://api.fxtwitter.com/status/{status_id}",
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        errors.append(f"FxTwitter returned HTTP {response.status_code}: {_response_detail(response)}")
        return None

    try:
        data = response.json()
    except ValueError as exc:
        errors.append(f"FxTwitter returned invalid JSON: {exc}")
        return None

    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        errors.append("FxTwitter response did not include tweet data")
        return None

    article = tweet.get("article")
    article_capture = _x_article_capture(article) if isinstance(article, dict) else None
    article_text = article_capture.text if article_capture else ""
    post_text = _x_post_text(tweet)
    if article_text:
        text = article_text
        content_source = "x_fxtwitter_article"
    elif post_text and not _is_link_only_text(post_text):
        text = post_text
        content_source = "x_fxtwitter"
    else:
        errors.append("FxTwitter returned only link placeholder text")
        return None

    author = tweet.get("author")
    author_name = author.get("name") if isinstance(author, dict) else tweet.get("user_name")
    author_screen_name = (
        author.get("screen_name") if isinstance(author, dict) else tweet.get("user_screen_name")
    )
    article_url = _x_article_url(article)
    metadata = {
        "social_platform": "x",
        "content_source": content_source,
        "captured_without_javascript": True,
        "provider_name": "FxTwitter",
        "provider_url": "https://api.fxtwitter.com",
        "author": author_name,
        "author_url": f"https://x.com/{author_screen_name}" if author_screen_name else None,
        "created_at": tweet.get("created_at"),
        "article_id": article.get("id") if isinstance(article, dict) else None,
        "article_title": article.get("title") if isinstance(article, dict) else None,
        "article_preview_text": article.get("preview_text") if isinstance(article, dict) else None,
        "article_url": article_url,
        "image_url": _x_article_image_url(article),
        "original_post_text": post_text,
    }
    video_urls = _x_video_urls(tweet)
    if video_urls:
        metadata["video_urls"] = video_urls
        metadata["primary_video_url"] = video_urls[0]
    if article_capture:
        metadata.update(
            {
                "article_block_count": article_capture.block_count,
                "article_text_length": len(article_capture.text),
                "article_used_preview_only": article_capture.used_preview_only,
            }
        )
        if article_capture.warning:
            metadata["article_capture_warning"] = article_capture.warning
    html = f"<article><h1>{_escape_text(metadata.get('article_title') or '')}</h1><p>{_escape_text(text)}</p></article>"
    return SocialPostCapture(text=text, html=html, metadata=_drop_empty(metadata))


def _capture_x_oembed(
    client: httpx.Client,
    url: str,
    errors: list[str],
) -> SocialPostCapture | None:
    response = client.get(
        "https://publish.twitter.com/oembed",
        params={
            "url": url,
            "omit_script": "1",
            "dnt": "true",
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        errors.append(f"X oEmbed returned HTTP {response.status_code}: {_response_detail(response)}")
        return None

    try:
        data = response.json()
    except ValueError as exc:
        errors.append(f"X oEmbed returned invalid JSON: {exc}")
        return None

    embed_html = str(data.get("html") or "")
    text = _html_to_text(embed_html)
    body_text = _first_paragraph_text(embed_html)
    if _is_link_only_text(body_text):
        errors.append("X oEmbed returned only a link placeholder")
        return None
    if _is_low_value_text(text):
        errors.append("X oEmbed returned no readable post text")
        return None

    metadata = {
        "social_platform": "x",
        "content_source": "x_oembed",
        "captured_without_javascript": True,
        "provider_name": data.get("provider_name") or "X",
        "provider_url": data.get("provider_url"),
        "author": data.get("author_name"),
        "author_url": data.get("author_url"),
        "oembed_cache_age": data.get("cache_age"),
        "oembed_html": embed_html,
    }
    return SocialPostCapture(text=text, html=embed_html, metadata=_drop_empty(metadata))


def _capture_facebook_oembed(
    client: httpx.Client,
    url: str,
    errors: list[str],
) -> SocialPostCapture | None:
    access_token = settings.facebook_oembed_access_token.strip()
    if not access_token:
        errors.append("FACEBOOK_OEMBED_ACCESS_TOKEN is not configured")
        return None

    graph_version = settings.facebook_graph_api_version.strip() or "v25.0"
    response = client.get(
        f"https://graph.facebook.com/{graph_version}/oembed_post",
        params={
            "url": url,
            "omitscript": "true",
            "access_token": access_token,
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        errors.append(f"Facebook oEmbed returned HTTP {response.status_code}: {_response_detail(response)}")
        return None

    try:
        data = response.json()
    except ValueError as exc:
        errors.append(f"Facebook oEmbed returned invalid JSON: {exc}")
        return None

    embed_html = str(data.get("html") or "")
    text = _html_to_text(embed_html)
    if _is_low_value_text(text):
        errors.append("Facebook oEmbed returned embed markup without readable post text")
        return None

    metadata = {
        "social_platform": "facebook",
        "content_source": "facebook_oembed",
        "captured_without_javascript": True,
        "provider_name": data.get("provider_name") or "Facebook",
        "provider_url": data.get("provider_url"),
        "oembed_html": embed_html,
        "oembed_width": data.get("width"),
        "oembed_height": data.get("height"),
    }
    return SocialPostCapture(text=text, html=embed_html, metadata=_drop_empty(metadata))


def _capture_static_metadata(
    client: httpx.Client,
    url: str,
    platform: str,
    errors: list[str],
) -> SocialPostCapture | None:
    response = client.get(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    if response.status_code >= 400:
        errors.append(f"static metadata fetch returned HTTP {response.status_code}: {_response_detail(response)}")
        return None

    page_metadata = _html_metadata(response.text)
    title = _first_value(
        page_metadata,
        "og:title",
        "twitter:title",
        "html_title",
    )
    description = _first_value(
        page_metadata,
        "og:description",
        "twitter:description",
        "description",
    )

    text_parts = [part for part in (title, description) if part and not _is_low_value_text(part)]
    text = _dedupe_lines("\n".join(text_parts))
    if _is_low_value_text(text):
        errors.append("static metadata did not include readable post text")
        return None

    metadata: dict[str, Any] = {
        "social_platform": platform,
        "content_source": f"{platform}_static_metadata",
        "captured_without_javascript": True,
        "title": title,
        "description": description,
        "canonical_url": _first_value(page_metadata, "og:url", "twitter:url"),
        "image_url": _first_value(page_metadata, "og:image", "twitter:image"),
    }
    if errors:
        metadata["social_capture_warnings"] = errors.copy()
    return SocialPostCapture(text=text, html=response.text, metadata=_drop_empty(metadata))


def _html_to_text(raw_html: str) -> str:
    parser = _TextHTMLParser()
    parser.feed(raw_html or "")
    return parser.text()


def _html_metadata(raw_html: str) -> dict[str, str]:
    parser = _MetadataHTMLParser()
    parser.feed(raw_html or "")
    return parser.metadata()


def _first_paragraph_text(raw_html: str) -> str:
    match = re.search(r"<p\b[^>]*>(.*?)</p>", raw_html or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _html_to_text(match.group(1))


def _x_status_id(url: str) -> str | None:
    match = _X_STATUS_RE.match(urlparse(url).path)
    if not match:
        return None
    return match.group(0).rstrip("/").split("/")[-1]


def _normalized_host(host: str) -> str:
    normalized = host.lower().split("@")[-1].split(":")[0]
    for prefix in ("www.", "mobile.", "m.", "web."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return normalized


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _dedupe_lines(value: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = _normalize_text(raw_line)
        key = line.casefold()
        if line and key not in seen:
            seen.add(key)
            lines.append(line)
    return "\n".join(lines)


def _x_article_capture(article: dict[str, Any]) -> _XArticleCapture | None:
    title = _normalize_text(str(article.get("title") or ""))
    preview = _normalize_text(str(article.get("preview_text") or ""))
    content = article.get("content")
    blocks = content.get("blocks") if isinstance(content, dict) else None
    entity_map = _x_article_entity_map(content)

    block_parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = _normalize_text(str(block.get("text") or ""))
            if text:
                block_parts.append(text)
            markdown = _x_article_markdown_entity_text(block, entity_map)
            if markdown:
                block_parts.append(markdown)

    block_text = _dedupe_article_parts(block_parts)
    used_preview_only = not block_text and bool(preview)
    article_body = block_text or preview
    if not article_body and not title:
        return None

    text = _dedupe_article_parts(part for part in (title, article_body) if part)
    warning = _x_article_capture_warning(
        preview=preview,
        block_text=block_text,
        block_count=len(block_parts),
        used_preview_only=used_preview_only,
    )
    return _XArticleCapture(
        text=text,
        block_text=block_text,
        block_count=len(block_parts),
        used_preview_only=used_preview_only,
        warning=warning,
    )


def _x_article_entity_map(content: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(content, dict):
        return {}

    raw_entity_map = content.get("entityMap")
    if isinstance(raw_entity_map, dict):
        return {
            str(key): value
            for key, value in raw_entity_map.items()
            if isinstance(value, dict)
        }
    if isinstance(raw_entity_map, list):
        entities: dict[str, dict[str, Any]] = {}
        for entry in raw_entity_map:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            value = entry.get("value")
            if key is not None and isinstance(value, dict):
                entities[str(key)] = value
        return entities
    return {}


def _x_article_markdown_entity_text(block: dict[str, Any], entity_map: dict[str, dict[str, Any]]) -> str:
    ranges = block.get("entityRanges")
    if not isinstance(ranges, list):
        return ""

    parts: list[str] = []
    for entity_range in ranges:
        if not isinstance(entity_range, dict):
            continue
        key = entity_range.get("key")
        entity = entity_map.get(str(key))
        if not entity or entity.get("type") != "MARKDOWN":
            continue
        data = entity.get("data")
        markdown = data.get("markdown") if isinstance(data, dict) else None
        if isinstance(markdown, str) and markdown.strip():
            parts.append(markdown.strip())
    return "\n\n".join(parts)


def _dedupe_article_parts(parts: Any) -> str:
    seen: set[str] = set()
    values: list[str] = []
    for part in parts:
        value = str(part or "").strip()
        key = _normalize_text(value).casefold()
        if value and key not in seen:
            seen.add(key)
            values.append(value)
    return "\n".join(values)


def _x_article_capture_warning(
    *,
    preview: str,
    block_text: str,
    block_count: int,
    used_preview_only: bool,
) -> str | None:
    if used_preview_only:
        return "FxTwitter article payload included preview text but no article content blocks"
    if not block_text:
        return None

    compact_body = _normalize_text(block_text)
    if len(compact_body) >= _X_ARTICLE_SUSPICIOUSLY_SHORT_CHARS:
        return None
    if _ends_like_complete_sentence(compact_body):
        return None

    if preview and len(preview) > len(compact_body):
        return "FxTwitter article content blocks are shorter than preview text and may be incomplete"
    if block_count <= 1:
        return "FxTwitter article content blocks are unusually short and may be incomplete"
    return None


def _x_post_text(tweet: dict[str, Any]) -> str:
    raw_text = tweet.get("raw_text")
    if isinstance(raw_text, dict):
        text = raw_text.get("text")
        if isinstance(text, str) and text.strip():
            return _normalize_text(text)
    text = tweet.get("text")
    if isinstance(text, str) and text.strip():
        return _normalize_text(text)
    return ""


def _x_article_url(article: Any) -> str | None:
    if not isinstance(article, dict):
        return None
    article_id = article.get("id")
    if article_id:
        return f"https://x.com/i/article/{article_id}"
    return None


def _x_article_image_url(article: Any) -> str | None:
    if not isinstance(article, dict):
        return None
    cover_media = article.get("cover_media")
    if not isinstance(cover_media, dict):
        return None
    media_info = cover_media.get("media_info")
    if not isinstance(media_info, dict):
        return None
    image_url = media_info.get("original_img_url")
    return str(image_url) if image_url else None


def _x_video_urls(tweet: dict[str, Any]) -> list[str]:
    """Extract direct video URLs from FxTwitter's tweet media payload.

    FxTwitter has returned a few compatible media shapes over time. Keep this
    intentionally narrow to Twitter/X video hosts and video-looking files so
    image previews are not accidentally sent through the media transcriber.
    """
    media = tweet.get("media")
    if not isinstance(media, dict):
        return []

    candidates: list[str] = []
    for key in ("videos", "all"):
        value = media.get(key)
        if isinstance(value, list):
            for entry in value:
                _collect_x_video_urls(entry, candidates)

    _collect_x_video_urls(media, candidates)
    seen: set[str] = set()
    urls: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return sorted(urls, key=_x_video_url_transcription_sort_key)


def _collect_x_video_urls(value: Any, candidates: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, str):
                if key in {"url", "video_url"} and _is_x_video_url(nested):
                    candidates.append(nested)
            elif isinstance(nested, (dict, list)):
                _collect_x_video_urls(nested, candidates)
    elif isinstance(value, list):
        for nested in value:
            _collect_x_video_urls(nested, candidates)


def _is_x_video_url(value: str) -> bool:
    parsed = urlparse(value)
    path = parsed.path.lower()
    if path.endswith((".mp4", ".m3u8", ".mov", ".webm")):
        return True
    return bool(_X_VIDEO_URL_RE.match(value)) and "/vid/" in path


def _x_video_url_transcription_sort_key(value: str) -> tuple[int, int, str]:
    parsed = urlparse(value)
    path = parsed.path.lower()
    if path.endswith(".mp4"):
        media_rank = 0
    elif path.endswith((".mov", ".webm")):
        media_rank = 1
    elif path.endswith(".m3u8"):
        media_rank = 2
    else:
        media_rank = 3

    resolution_match = re.search(r"/(\d+)x(\d+)/", path)
    if resolution_match:
        width = int(resolution_match.group(1))
        height = int(resolution_match.group(2))
        pixels = width * height
    else:
        pixels = 999_999_999

    return (media_rank, pixels, value)


def _is_link_only_text(value: str | None) -> bool:
    text = _normalize_text(value or "")
    if not text:
        return False
    without_urls = re.sub(r"https?://\S+", "", text)
    without_urls = re.sub(r"\s+", " ", without_urls).strip(" -—–|·.,;:()[]{}")
    return not without_urls


def _ends_like_complete_sentence(value: str) -> bool:
    return bool(re.search(r'[.!?]"?\)?$', _normalize_text(value)))


def _escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _first_value(metadata: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value:
            return value
    return None


def _drop_empty(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if value not in (None, "", [], {})}


def _is_low_value_text(value: str | None) -> bool:
    text = _normalize_text(value or "")
    if len(text) < 12:
        return True
    lower_text = text.casefold()
    return any(marker in lower_text for marker in _LOW_VALUE_TEXT_MARKERS)


def _response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _normalize_text(response.text)[:240]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)[:240]
    return _normalize_text(str(data))[:240]
