"""The ARQ queue must be a data channel, never a code channel.

ARQ's default serializer is ``pickle``, so anything able to LPUSH onto the queue
gets code execution inside a worker (CWE-502). These tests pin the JSON
replacement: full round-trip fidelity for the values the app enqueues, and a
hard refusal of anything that is not our own wire format.
"""

from __future__ import annotations

import pickle
from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.workers.serialization import job_deserializer, job_serializer


def _round_trip(payload: dict) -> dict:
    return job_deserializer(job_serializer(payload))


def test_job_payload_round_trips_with_full_fidelity() -> None:
    payload = {
        "f": "refresh_source_resource",
        "a": (1, "two", None, True),
        "k": {
            "when": datetime(2026, 8, 8, 12, 30, tzinfo=timezone.utc),
            "day": date(2026, 8, 8),
            "at": time(12, 30, 15),
            "item_id": UUID("11111111-2222-3333-4444-555555555555"),
            "amount": Decimal("10.25"),
            "blob": b"\x00\xff binary",
            "tags": {"alpha", "beta"},
            "frozen": frozenset({1, 2}),
            "nested": {"list": [1, {"deep": (2, 3)}]},
        },
        "t": 0,
        "et": None,
    }

    assert _round_trip(payload) == payload
    # The tuple must survive as a tuple: ARQ splats it as positional args.
    assert isinstance(_round_trip(payload)["a"], tuple)


def test_wire_format_is_plain_json() -> None:
    raw = job_serializer({"f": "task", "a": (1,), "k": {}})

    assert raw.startswith(b"{")
    assert b"__arq__" in raw  # the tuple tag, not a pickle opcode
    assert b"\x80" not in raw  # no pickle protocol marker


def test_dict_keys_that_collide_with_the_type_tag_still_round_trip() -> None:
    payload = {"k": {"__arq__": "not-a-tag", "v": 1}}

    assert _round_trip(payload) == payload


def test_non_string_dict_keys_round_trip() -> None:
    payload = {"k": {1: "one", (2, 3): "pair"}}

    assert _round_trip(payload) == payload


def test_unserializable_values_degrade_to_repr_instead_of_raising() -> None:
    # ARQ reuses this serializer for job *results*, and a failed job's result is
    # the raised exception. Raising here would hide the real error.
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    decoded = _round_trip({"r": Opaque()})

    assert decoded["r"] == "Opaque: <opaque>"


def test_unserializable_value_message_scrubs_embedded_dsn_credentials() -> None:
    # B-08: a failed job's result is the raised exception, and connection
    # failures routinely quote the DSN they tried to reach -- credentials
    # included -- verbatim in the message. That must not reach Valkey intact.
    class ConnectionFailure:
        def __str__(self) -> str:
            return "could not connect to postgresql://palaceoftruth:s3cret-pw@postgres:5432/palaceoftruth"

    decoded = _round_trip({"r": ConnectionFailure()})

    assert "s3cret-pw" not in decoded["r"]
    assert "postgresql://palaceoftruth:***@postgres:5432/palaceoftruth" in decoded["r"]


def test_pickle_payloads_are_rejected() -> None:
    # The exact attack shape from the finding: a crafted pickle LPUSHed onto the
    # queue. It must fail to decode, not execute.
    with pytest.raises(Exception):
        job_deserializer(pickle.dumps({"f": "os.system", "a": ("id",)}))


def test_unknown_tags_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown ARQ payload tag"):
        job_deserializer(b'{"k":{"__arq__":"exec","v":"id"}}')


def test_non_mapping_payloads_are_rejected() -> None:
    with pytest.raises(ValueError, match="must decode to a mapping"):
        job_deserializer(b"[1, 2, 3]")
