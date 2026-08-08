"""JSON job serialization for ARQ.

ARQ serializes both job payloads and job results with ``pickle`` by default, so
anything that can write to the Redis/Valkey queue gets arbitrary code execution
inside a worker the moment the job is picked up (CWE-502). These serializers
keep the queue a pure data channel: a crafted queue entry can at worst produce a
``DeserializationError``.

The payloads ARQ stores are plain dicts of job metadata plus the caller's args,
kwargs and result. JSON covers almost all of that directly; the small set of
Python types the codebase legitimately puts on the queue (datetimes, UUIDs,
tuples, sets, bytes, Decimal) is round-tripped through an explicit type tag.

Wire format: JSON, where a dict of the form ``{"__arq__": <tag>, "v": <value>}``
denotes a tagged non-JSON-native value. Plain dicts that happen to contain the
``__arq__`` key are themselves escaped into the tagged ``map`` form, so decoding
is unambiguous in both directions.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Reserved keys for the tagged-value envelope.
_TAG = "__arq__"
_VALUE = "v"


def _encode(obj: Any) -> Any:
    """Recursively convert ``obj`` into JSON-native data."""
    # bool is a subclass of int, and both encode natively — no ordering concern.
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        # Fast path: a plain JSON object. Anything else (non-string keys, or a
        # key that would collide with the type tag) goes through the pair form.
        if _TAG not in obj and all(isinstance(key, str) for key in obj):
            return {key: _encode(value) for key, value in obj.items()}
        return {_TAG: "map", _VALUE: [[_encode(key), _encode(value)] for key, value in obj.items()]}
    if isinstance(obj, list):
        return [_encode(value) for value in obj]
    if isinstance(obj, tuple):
        # ARQ stores positional args as a tuple and JSON has no tuple type; tag
        # it so the worker calls the task function with the same shape.
        return {_TAG: "tuple", _VALUE: [_encode(value) for value in obj]}
    if isinstance(obj, set):
        return {_TAG: "set", _VALUE: [_encode(value) for value in obj]}
    if isinstance(obj, frozenset):
        return {_TAG: "frozenset", _VALUE: [_encode(value) for value in obj]}
    # datetime must precede date: datetime is a date subclass.
    if isinstance(obj, datetime):
        return {_TAG: "datetime", _VALUE: obj.isoformat()}
    if isinstance(obj, date):
        return {_TAG: "date", _VALUE: obj.isoformat()}
    if isinstance(obj, time):
        return {_TAG: "time", _VALUE: obj.isoformat()}
    if isinstance(obj, UUID):
        return {_TAG: "uuid", _VALUE: str(obj)}
    if isinstance(obj, Decimal):
        return {_TAG: "decimal", _VALUE: str(obj)}
    if isinstance(obj, (bytes, bytearray)):
        return {_TAG: "bytes", _VALUE: base64.b64encode(bytes(obj)).decode("ascii")}
    # Unsupported types are degraded to their repr rather than raising. ARQ uses
    # this same serializer for job *results*, and a failed job's result is the
    # raised exception — refusing to encode it would replace the real error with
    # ARQ's generic "unable to serialize result" placeholder and lose the cause.
    logger.warning("ARQ payload contains non-JSON-serializable %s; storing repr", type(obj).__name__)
    return {_TAG: "repr", _VALUE: f"{type(obj).__name__}: {obj!r}"}


def _decode(obj: Any) -> Any:
    """Recursively rebuild Python values from :func:`_encode` output."""
    if isinstance(obj, list):
        return [_decode(value) for value in obj]
    if not isinstance(obj, dict):
        return obj

    tag = obj.get(_TAG)
    if tag is None:
        return {key: _decode(value) for key, value in obj.items()}

    value = obj.get(_VALUE)
    if tag == "map":
        return {_decode(key): _decode(item) for key, item in value}
    if tag == "tuple":
        return tuple(_decode(item) for item in value)
    if tag == "set":
        return {_decode(item) for item in value}
    if tag == "frozenset":
        return frozenset(_decode(item) for item in value)
    if tag == "datetime":
        return datetime.fromisoformat(value)
    if tag == "date":
        return date.fromisoformat(value)
    if tag == "time":
        return time.fromisoformat(value)
    if tag == "uuid":
        return UUID(value)
    if tag == "decimal":
        return Decimal(value)
    if tag == "bytes":
        return base64.b64decode(value)
    if tag == "repr":
        # Encoded lossily on the way out; the string is all that survives.
        return value
    raise ValueError(f"unknown ARQ payload tag {tag!r}")


def job_serializer(data: dict[str, Any]) -> bytes:
    """Serialize an ARQ job/result payload to JSON bytes."""
    return json.dumps(_encode(data), separators=(",", ":")).encode("utf-8")


def job_deserializer(payload: bytes) -> dict[str, Any]:
    """Deserialize an ARQ job/result payload from JSON bytes.

    Raises on anything that is not our own JSON wire format. ARQ wraps the
    failure in ``DeserializationError``; it never executes the payload.
    """
    decoded = _decode(json.loads(payload))
    if not isinstance(decoded, dict):
        raise ValueError("ARQ payload must decode to a mapping")
    return decoded
