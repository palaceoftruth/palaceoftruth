from types import SimpleNamespace

import pytest

from app.pipelines.webpage import _guard_browser_request


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.aborted: str | None = None
        self.continued = False

    async def abort(self, reason: str) -> None:
        self.aborted = reason

    async def continue_(self) -> None:
        self.continued = True


@pytest.mark.asyncio
async def test_browser_guard_aborts_non_http_scheme() -> None:
    route = _Route("file:///etc/passwd")

    await _guard_browser_request(route, source_url="https://example.com")

    assert route.aborted == "blockedbyclient"
    assert route.continued is False
