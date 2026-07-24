import pytest

from scripts.seed_watched_source_canary import (
    DEFAULT_HOST,
    DEFAULT_TENANT_ID,
    DEFAULT_URL,
    parse_args,
)


def test_canary_seed_defaults_are_internal_and_zero_write() -> None:
    args = parse_args([])

    assert args.tenant_id == DEFAULT_TENANT_ID
    assert args.url == DEFAULT_URL
    assert args.allowed_host == DEFAULT_HOST
    assert args.write is False
    assert args.refresh_slo_seconds == 900


@pytest.mark.parametrize(
    "argv",
    [
        ["--url", "https://outside.example/canary.html"],
        ["--tenant-id", "default"],
        ["--refresh-slo-seconds", "60"],
        ["--refresh-slo-seconds", "7200"],
    ],
)
def test_canary_seed_rejects_scope_expansion(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)
