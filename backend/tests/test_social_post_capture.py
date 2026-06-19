import asyncio

import httpx
import pytest

from app.config import settings
from app.pipelines import webpage as webpage_module
from app.pipelines.social import (
    SocialCaptureError,
    SocialPostCapture,
    capture_social_post,
    detect_social_post_platform,
)
from app.pipelines.webpage import WebpagePipeline


class FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_detect_social_post_platform_only_matches_single_post_urls() -> None:
    assert detect_social_post_platform("https://x.com/Interior/status/463440424141459456") == "x"
    assert detect_social_post_platform("https://twitter.com/Interior/statuses/463440424141459456") == "x"
    assert detect_social_post_platform("https://x.com/Interior") is None

    assert detect_social_post_platform("https://www.facebook.com/example/posts/pfbid123") == "facebook"
    assert detect_social_post_platform("https://m.facebook.com/story.php?story_fbid=123&id=456") == "facebook"
    assert detect_social_post_platform("https://www.facebook.com/example") is None


def test_capture_x_post_uses_oembed_without_javascript() -> None:
    client = FakeClient(
        [
            httpx.Response(404, json={"message": "not found"}),
            httpx.Response(
                200,
                json={
                    "provider_name": "Twitter",
                    "provider_url": "https://twitter.com",
                    "author_name": "US Department of the Interior",
                    "author_url": "https://twitter.com/Interior",
                    "cache_age": "3153600000",
                    "html": (
                        '<blockquote class="twitter-tweet">'
                        '<p lang="en" dir="ltr">Sunsets over '
                        '<a href="https://twitter.com/GrandTetonNPS">@GrandTetonNPS</a>.</p>'
                        "- US Department of the Interior (@Interior)"
                        "</blockquote>"
                    ),
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/Interior/status/463440424141459456", client=client)

    assert capture is not None
    assert capture.text == "Sunsets over @GrandTetonNPS . - US Department of the Interior (@Interior)"
    assert capture.metadata["content_source"] == "x_oembed"
    assert capture.metadata["captured_without_javascript"] is True
    assert capture.metadata["author"] == "US Department of the Interior"
    assert client.calls == [
        {
            "url": "https://api.fxtwitter.com/status/463440424141459456",
            "headers": {"Accept": "application/json"},
        },
        {
            "url": "https://publish.twitter.com/oembed",
            "params": {
                "url": "https://x.com/Interior/status/463440424141459456",
                "omit_script": "1",
                "dnt": "true",
            },
            "headers": {"Accept": "application/json"},
        }
    ]


def test_capture_x_article_uses_fxtwitter_article_body_before_oembed() -> None:
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "code": 200,
                    "tweet": {
                        "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
                        "id": "2051708305819435445",
                        "raw_text": {"text": "https://t.co/qGhDwxQeho"},
                        "created_at": "Tue May 05 16:59:13 +0000 2026",
                        "author": {
                            "name": "Zephyr",
                            "screen_name": "Zephyr_hg",
                        },
                        "article": {
                            "id": "2051707695070138372",
                            "title": "The 4 AI Workflows Most Creators Don't Teach",
                            "preview_text": "Most creators teach you how to build an audience.",
                            "cover_media": {
                                "media_info": {
                                    "original_img_url": "https://pbs.twimg.com/media/example.jpg",
                                }
                            },
                            "content": {
                                "blocks": [
                                    {
                                        "type": "unstyled",
                                        "text": "Most creators teach you how to build an audience and create content.",
                                    },
                                    {
                                        "type": "header-two",
                                        "text": "Why These 4 Aren't Taught",
                                    },
                                    {
                                        "type": "atomic",
                                        "text": " ",
                                        "entityRanges": [{"key": 3, "offset": 0, "length": 1}],
                                    },
                                    {
                                        "type": "atomic",
                                        "text": " ",
                                        "entityRanges": [{"key": 4, "offset": 0, "length": 1}],
                                    },
                                ],
                                "entityMap": [
                                    {
                                        "key": "3",
                                        "value": {
                                            "type": "MARKDOWN",
                                            "mutability": "Mutable",
                                            "data": {
                                                "markdown": "```\nconst statusTax = false;\n```",
                                            },
                                        },
                                    },
                                    {
                                        "key": "4",
                                        "value": {
                                            "type": "DIVIDER",
                                            "mutability": "Immutable",
                                            "data": {},
                                        },
                                    },
                                ],
                            },
                        },
                    },
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/Zephyr_hg/status/2051708305819435445", client=client)

    assert capture is not None
    assert capture.metadata["content_source"] == "x_fxtwitter_article"
    assert capture.metadata["article_url"] == "https://x.com/i/article/2051707695070138372"
    assert capture.metadata["image_url"] == "https://pbs.twimg.com/media/example.jpg"
    assert capture.metadata["original_post_text"] == "https://t.co/qGhDwxQeho"
    assert capture.metadata["article_block_count"] == 3
    assert capture.metadata["article_text_length"] == len(capture.text)
    assert "article_capture_warning" not in capture.metadata
    assert "https://t.co/qGhDwxQeho" not in capture.text
    assert "The 4 AI Workflows Most Creators Don't Teach" in capture.text
    assert "Most creators teach you how to build an audience and create content." in capture.text
    assert "Why These 4 Aren't Taught" in capture.text
    assert "```\nconst statusTax = false;\n```" in capture.text
    assert "DIVIDER" not in capture.text
    assert client.calls == [
        {
            "url": "https://api.fxtwitter.com/status/2051708305819435445",
            "headers": {"Accept": "application/json"},
        }
    ]


def test_capture_x_post_records_video_urls_from_fxtwitter_media() -> None:
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "code": 200,
                    "tweet": {
                        "url": "https://x.com/example/status/2051708305819435445",
                        "id": "2051708305819435445",
                        "text": "A demo video from the field.",
                        "author": {"name": "Video Author", "screen_name": "example"},
                        "media": {
                            "videos": [
                                {
                                    "url": "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/1280x720/demo.mp4?tag=12",
                                    "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/demo.jpg",
                                },
                                {
                                    "url": "https://video.twimg.com/ext_tw_video/2051708305819435445/pl/playlist.m3u8?tag=12",
                                },
                                {
                                    "url": "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/480x270/demo.mp4?tag=12",
                                }
                            ],
                            "photos": [
                                {"url": "https://pbs.twimg.com/media/example.jpg"}
                            ],
                        },
                    },
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/example/status/2051708305819435445", client=client)

    assert capture is not None
    assert capture.text == "A demo video from the field."
    assert capture.metadata["video_urls"] == [
        "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/480x270/demo.mp4?tag=12",
        "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/1280x720/demo.mp4?tag=12",
        "https://video.twimg.com/ext_tw_video/2051708305819435445/pl/playlist.m3u8?tag=12",
    ]
    assert capture.metadata["primary_video_url"] == (
        "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/480x270/demo.mp4?tag=12"
    )
    assert "https://pbs.twimg.com/media/example.jpg" not in capture.metadata["video_urls"]


def test_capture_x_post_prefers_smallest_direct_mp4_for_transcription() -> None:
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "code": 200,
                    "tweet": {
                        "url": "https://x.com/example/status/2056887572966834644",
                        "id": "2056887572966834644",
                        "text": "A multi-variant video.",
                        "author": {"name": "Video Author", "screen_name": "example"},
                        "media": {
                            "videos": [
                                {
                                    "url": "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/3840x2160/pY5NzeoDVqOFca55.mp4?tag=27",
                                },
                                {
                                    "url": "https://video.twimg.com/amplify_video/2056887046808166401/pl/kUW3CKgwy9_F8JNp.m3u8?tag=27&v=6b5",
                                },
                                {
                                    "url": "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/480x270/OaCHRYDieXcnFKef.mp4?tag=27",
                                },
                                {
                                    "url": "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/1280x720/F_zTDXM_syix2fqo.mp4?tag=27",
                                },
                            ],
                        },
                    },
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/example/status/2056887572966834644", client=client)

    assert capture is not None
    assert capture.metadata["video_urls"] == [
        "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/480x270/OaCHRYDieXcnFKef.mp4?tag=27",
        "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/1280x720/F_zTDXM_syix2fqo.mp4?tag=27",
        "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/3840x2160/pY5NzeoDVqOFca55.mp4?tag=27",
        "https://video.twimg.com/amplify_video/2056887046808166401/pl/kUW3CKgwy9_F8JNp.m3u8?tag=27&v=6b5",
    ]
    assert capture.metadata["primary_video_url"] == (
        "https://video.twimg.com/amplify_video/2056887046808166401/vid/avc1/480x270/OaCHRYDieXcnFKef.mp4?tag=27"
    )
    assert (
        "https://video.twimg.com/ext_tw_video/2051708305819435445/pu/vid/avc1/1280x720/demo.mp4?tag=12"
        not in capture.metadata["video_urls"]
    )


def test_capture_x_article_preview_only_payload_marks_incomplete_capture() -> None:
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "code": 200,
                    "tweet": {
                        "url": "https://x.com/kiruwaaaaaa/status/2052447866208047296",
                        "id": "2052447866208047296",
                        "raw_text": {"text": "https://t.co/providerLag"},
                        "author": {"name": "Operator Example", "screen_name": "kiruwaaaaaa"},
                        "article": {
                            "id": "2052409139591012355",
                            "title": "Provider lag example",
                            "preview_text": "Preview text arrived before the full X Article blocks were available.",
                            "content": {"blocks": []},
                        },
                    },
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/kiruwaaaaaa/status/2052447866208047296", client=client)

    assert capture is not None
    assert capture.text == (
        "Provider lag example\n"
        "Preview text arrived before the full X Article blocks were available."
    )
    assert capture.metadata["content_source"] == "x_fxtwitter_article"
    assert capture.metadata["article_url"] == "https://x.com/i/article/2052409139591012355"
    assert capture.metadata["article_block_count"] == 0
    assert capture.metadata["article_used_preview_only"] is True
    assert capture.metadata["article_capture_warning"] == (
        "FxTwitter article payload included preview text but no article content blocks"
    )
    assert capture.metadata["original_post_text"] == "https://t.co/providerLag"


def test_capture_x_article_short_blocks_warn_without_falling_back_to_link_only_status() -> None:
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "code": 200,
                    "tweet": {
                        "url": "https://x.com/essay/status/2052447866208047296",
                        "id": "2052447866208047296",
                        "text": "https://t.co/articleOnly",
                        "author": {"name": "Essay Author", "screen_name": "essay"},
                        "article": {
                            "id": "2052409139591012355",
                            "title": "A long article that has not fully loaded",
                            "preview_text": (
                                "This preview is longer than the blocks and suggests the provider "
                                "returned an early partial article payload."
                            ),
                            "content": {
                                "blocks": [
                                    {
                                        "type": "unstyled",
                                        "text": "This article starts but stops mid-thought",
                                    }
                                ]
                            },
                        },
                    },
                },
            )
        ]
    )

    capture = capture_social_post("https://x.com/essay/status/2052447866208047296", client=client)

    assert capture is not None
    assert capture.metadata["content_source"] == "x_fxtwitter_article"
    assert capture.metadata["article_block_count"] == 1
    assert capture.metadata["article_used_preview_only"] is False
    assert capture.metadata["article_capture_warning"] == (
        "FxTwitter article content blocks are shorter than preview text and may be incomplete"
    )
    assert capture.metadata["original_post_text"] == "https://t.co/articleOnly"
    assert "https://t.co/articleOnly" not in capture.text
    assert "This article starts but stops mid-thought" in capture.text


def test_capture_x_link_only_oembed_is_not_treated_as_readable_content() -> None:
    client = FakeClient(
        [
            httpx.Response(404, json={"message": "not found"}),
            httpx.Response(
                200,
                json={
                    "author_name": "Zephyr",
                    "html": (
                        '<blockquote class="twitter-tweet">'
                        '<p lang="zxx" dir="ltr">'
                        '<a href="https://t.co/qGhDwxQeho">https://t.co/qGhDwxQeho</a>'
                        "</p>"
                        "&mdash; Zephyr (@Zephyr_hg) "
                        '<a href="https://twitter.com/Zephyr_hg/status/2051708305819435445">'
                        "May 5, 2026</a></blockquote>"
                    ),
                },
            ),
            httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <meta property="og:title" content="X">
                    <meta property="og:description" content="X. It's what's happening">
                  </head>
                </html>
                """,
            ),
        ]
    )

    with pytest.raises(SocialCaptureError, match="link placeholder"):
        capture_social_post("https://x.com/Zephyr_hg/status/2051708305819435445", client=client)


def test_capture_facebook_post_falls_back_to_static_metadata_without_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "facebook_oembed_access_token", "")
    client = FakeClient(
        [
            httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>Fallback title</title>
                    <meta property="og:title" content="Launch update">
                    <meta property="og:description" content="We shipped the public beta today.">
                    <meta property="og:url" content="https://www.facebook.com/example/posts/pfbid123">
                  </head>
                </html>
                """,
            )
        ]
    )

    capture = capture_social_post("https://www.facebook.com/example/posts/pfbid123", client=client)

    assert capture is not None
    assert capture.text == "Launch update\nWe shipped the public beta today."
    assert capture.metadata["content_source"] == "facebook_static_metadata"
    assert capture.metadata["canonical_url"] == "https://www.facebook.com/example/posts/pfbid123"
    assert capture.metadata["social_capture_warnings"] == [
        "FACEBOOK_OEMBED_ACCESS_TOKEN is not configured"
    ]
    assert client.calls == [
        {
            "url": "https://www.facebook.com/example/posts/pfbid123",
            "headers": {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            },
        }
    ]


def test_capture_facebook_uses_static_metadata_when_oembed_has_no_text(monkeypatch) -> None:
    monkeypatch.setattr(settings, "facebook_oembed_access_token", "app-token")
    monkeypatch.setattr(settings, "facebook_graph_api_version", "v25.0")
    client = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "provider_name": "Facebook",
                    "provider_url": "https://www.facebook.com",
                    "html": '<iframe src="https://www.facebook.com/plugins/post.php"></iframe>',
                },
            ),
            httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <meta property="og:title" content="Public page post">
                    <meta property="og:description" content="A readable public post body.">
                  </head>
                </html>
                """,
            ),
        ]
    )

    capture = capture_social_post("https://www.facebook.com/example/posts/pfbid123", client=client)

    assert capture is not None
    assert capture.text == "Public page post\nA readable public post body."
    assert capture.metadata["social_capture_warnings"] == [
        "Facebook oEmbed returned embed markup without readable post text"
    ]
    assert client.calls[0]["url"] == "https://graph.facebook.com/v25.0/oembed_post"
    assert client.calls[0]["params"] == {
        "url": "https://www.facebook.com/example/posts/pfbid123",
        "omitscript": "true",
        "access_token": "app-token",
    }


def test_capture_social_post_raises_when_provider_metadata_has_no_readable_text(monkeypatch) -> None:
    monkeypatch.setattr(settings, "facebook_oembed_access_token", "")
    client = FakeClient(
        [
            httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <meta property="og:title" content="Facebook">
                    <meta property="og:description" content="Log in to Facebook to see posts, photos and more.">
                  </head>
                </html>
                """,
            )
        ]
    )

    with pytest.raises(SocialCaptureError, match="FACEBOOK_OEMBED_ACCESS_TOKEN"):
        capture_social_post("https://www.facebook.com/example/posts/pfbid123", client=client)


def test_webpage_scrape_returns_social_capture_before_article_scraper(monkeypatch) -> None:
    expected = SocialPostCapture(
        text="Captured social text",
        html="<blockquote>Captured social text</blockquote>",
        metadata={"content_source": "x_oembed", "captured_without_javascript": True},
    )

    monkeypatch.setattr(webpage_module, "capture_social_post", lambda _url: expected)

    def fail_fetch_url(_url: str) -> str:
        raise AssertionError("article scraper should not run after social capture succeeds")

    monkeypatch.setattr(webpage_module.trafilatura, "fetch_url", fail_fetch_url)

    html, text, metadata = WebpagePipeline._scrape("https://x.com/Interior/status/463440424141459456")

    assert html == "<blockquote>Captured social text</blockquote>"
    assert text == "Captured social text"
    assert metadata == {"content_source": "x_oembed", "captured_without_javascript": True}


def test_webpage_extract_appends_social_video_transcript(monkeypatch) -> None:
    expected = SocialPostCapture(
        text="Captured social text",
        html="<blockquote>Captured social text</blockquote>",
        metadata={
            "content_source": "x_fxtwitter",
            "primary_video_url": "https://video.twimg.com/ext_tw_video/demo.mp4",
        },
    )

    async def fake_media_extract(self, url: str, job_id: str = "unknown"):
        assert url == "https://video.twimg.com/ext_tw_video/demo.mp4"
        assert job_id == "job-123"
        return "Video words from the attached clip.", {"duration_seconds": 12}

    monkeypatch.setattr(webpage_module, "capture_social_post", lambda _url: expected)
    monkeypatch.setattr(webpage_module.MediaPipeline, "extract", fake_media_extract)

    pipeline = WebpagePipeline(db=None, embedder=None, llm=None)
    text, metadata = asyncio.run(
        pipeline.extract("https://x.com/example/status/2051708305819435445", job_id="job-123")
    )

    assert text == (
        "Captured social text\n\n---\n\n"
        "Attached Video Transcript:\nVideo words from the attached clip."
    )
    assert metadata["social_video_transcribed"] is True
    assert metadata["social_video_transcript_url"] == "https://video.twimg.com/ext_tw_video/demo.mp4"
    assert metadata["social_video_metadata"] == {"duration_seconds": 12}


def test_webpage_extract_does_not_use_browser_for_failed_social_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        WebpagePipeline,
        "_scrape",
        staticmethod(lambda _url: (None, None, {"social_capture_error": "oEmbed unavailable"})),
    )

    async def fail_browser(self, url: str):
        raise AssertionError(f"browser should not run for social URL {url}")

    monkeypatch.setattr(WebpagePipeline, "_scrape_with_browser", fail_browser)

    pipeline = WebpagePipeline(db=None, embedder=None, llm=None)
    with pytest.raises(ValueError, match="without JavaScript: oEmbed unavailable"):
        asyncio.run(pipeline.extract("https://x.com/Interior/status/463440424141459456"))
