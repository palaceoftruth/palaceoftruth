import pytest

from app.services.watched_source_discovery import (
    MAX_DISCOVERY_DOCUMENT_BYTES,
    SOURCE_CLASS_POLICIES,
    discover_feed_candidates,
    discover_sitemap_candidates,
)


def test_feed_discovery_is_allowlisted_bounded_and_does_not_subscribe_websub() -> None:
    body = """<?xml version="1.0"?><rss><channel>
      <link rel="hub" href="https://example.com/hub" />
      <item><link>https://example.com/one</link></item>
      <item><link>https://outside.example/two</link></item>
      <item><link>https://example.com/one</link></item>
    </channel></rss>"""
    result = discover_feed_candidates(
        feed_url="https://example.com/feed.xml",
        body=body,
        allowed_hosts=["example.com"],
        max_candidates=2,
    )
    assert [candidate.url for candidate in result.candidates] == ["https://example.com/one"]
    assert result.advertised_websub_hubs == ("https://example.com/hub",)
    assert result.rejected == 2


def test_sitemap_discovery_rejects_cross_host_and_caps_results() -> None:
    body = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>/one</loc></url><url><loc>https://example.com/two</loc></url>
      <url><loc>https://elsewhere.example/nope</loc></url>
    </urlset>"""
    result = discover_sitemap_candidates(
        sitemap_url="https://example.com/sitemap.xml",
        body=body,
        allowed_hosts=["example.com"],
        max_candidates=1,
    )
    assert [candidate.url for candidate in result.candidates] == ["https://example.com/one"]
    # The parser stops immediately at the accepted canary cap instead of
    # walking later, attacker-controlled sitemap entries.
    assert result.rejected == 0


def test_discovery_requires_an_explicit_allowlist_and_valid_xml() -> None:
    with pytest.raises(ValueError, match="allowed_hosts"):
        discover_feed_candidates(feed_url="https://example.com/feed", body="", allowed_hosts=[])
    with pytest.raises(ValueError, match="valid XML"):
        discover_sitemap_candidates(
            sitemap_url="https://example.com/sitemap.xml", body="not xml", allowed_hosts=["example.com"]
        )


def test_discovery_rejects_oversized_or_entity_declaring_documents() -> None:
    with pytest.raises(ValueError, match="maximum size"):
        discover_feed_candidates(
            feed_url="https://example.com/feed", body=b"x" * (MAX_DISCOVERY_DOCUMENT_BYTES + 1), allowed_hosts=["example.com"]
        )
    with pytest.raises(ValueError, match="declarations"):
        discover_sitemap_candidates(
            sitemap_url="https://example.com/sitemap.xml", body="<!DOCTYPE x><urlset/>", allowed_hosts=["example.com"]
        )


def test_source_class_policies_have_safe_adaptive_bounds() -> None:
    for policy in SOURCE_CLASS_POLICIES.values():
        assert policy.minimum_interval <= policy.refresh_slo <= policy.maximum_interval
