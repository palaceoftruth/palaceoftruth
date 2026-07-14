import importlib.util
import json
from email.message import Message
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "list_oci_tags.py"
SPEC = importlib.util.spec_from_file_location("list_oci_tags", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
list_oci_tags_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(list_oci_tags_module)
list_oci_tags = list_oci_tags_module.list_oci_tags
HELM_SEMVER_TAG_RE = list_oci_tags_module.HELM_SEMVER_TAG_RE


class FakeResponse:
    def __init__(
        self,
        tags: list[str],
        *,
        link: str | None = None,
        body: object | None = None,
    ) -> None:
        self._body = json.dumps({"tags": tags} if body is None else body).encode()
        self.headers = Message()
        if link:
            self.headers["Link"] = link

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_list_oci_tags_follows_every_page_and_deduplicates() -> None:
    responses = iter(
        [
            FakeResponse(
                ["0.1.477", "latest"],
                link='</v2/palace/chart/tags/list?n=2&last=latest>; rel="next"',
            ),
            FakeResponse(["0.1.478", "0.1.477"]),
        ]
    )

    tags = list_oci_tags(
        "https://ghcr.io/v2/palace/chart/tags/list?n=2",
        "secret",
        opener=lambda request, timeout: next(responses),
    )

    assert tags == ["0.1.477", "0.1.478", "latest"]


def test_list_oci_tags_rejects_cross_origin_pagination() -> None:
    response = FakeResponse(
        ["0.1.477"],
        link='<https://example.com/v2/palace/chart/tags/list>; rel="next"',
    )

    with pytest.raises(ValueError, match="changed origin"):
        list_oci_tags(
            "https://ghcr.io/v2/palace/chart/tags/list?n=100",
            "secret",
            opener=lambda request, timeout: response,
        )


def test_list_oci_tags_rejects_non_object_payload() -> None:
    response = FakeResponse([], body=[])

    with pytest.raises(ValueError, match="invalid tags payload"):
        list_oci_tags(
            "https://ghcr.io/v2/palace/chart/tags/list?n=100",
            "secret",
            opener=lambda request, timeout: response,
        )


def test_list_oci_tags_rejects_payload_without_tags() -> None:
    response = FakeResponse([], body={"name": "palace/chart"})

    with pytest.raises(ValueError, match="invalid tags payload"):
        list_oci_tags(
            "https://ghcr.io/v2/palace/chart/tags/list?n=100",
            "secret",
            opener=lambda request, timeout: response,
        )


def test_list_oci_tags_requires_bearer_token() -> None:
    with pytest.raises(ValueError, match="bearer token"):
        list_oci_tags("https://ghcr.io/v2/palace/chart/tags/list?n=100", "")


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [
        ("0.1.478", True),
        ("0.1.478-rc.1", True),
        ("0.1.478_build.1", True),
        ("latest", False),
        ("sha-abc12345", False),
    ],
)
def test_helm_semver_tag_filter(tag: str, accepted: bool) -> None:
    assert bool(HELM_SEMVER_TAG_RE.fullmatch(tag)) is accepted
