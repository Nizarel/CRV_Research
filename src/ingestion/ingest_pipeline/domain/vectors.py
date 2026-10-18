"""Strict reconciliation of embedding responses against the inputs that were sent."""

from __future__ import annotations

import math
import sys
from array import array
from collections.abc import Iterable, Sequence

from .errors import IntegrityError

Vector = tuple[float, ...]

VECTOR_ENCODING = "f32le"


def encode_f32le(vector: Sequence[float]) -> bytes:
    values = array("f", vector)
    if sys.byteorder == "big":
        values.byteswap()
    return values.tobytes()


def decode_f32le(data: bytes, *, dimensions: int) -> Vector:
    if len(data) != 4 * dimensions:
        raise IntegrityError(f"Encoded vector has {len(data)} bytes, expected {4 * dimensions}")
    values = array("f")
    values.frombytes(data)
    if sys.byteorder == "big":
        values.byteswap()
    return tuple(values)


def reconcile_embeddings(
    expected: int,
    items: Iterable[tuple[int, Sequence[float]]],
    *,
    dimensions: int,
) -> list[Vector]:
    """Return one validated vector per input, in input order, or raise ``IntegrityError``.

    Rejects missing, duplicate and out-of-range indexes, wrong dimensions, non-finite values
    and all-zero vectors, so an incomplete response can never be stored as an empty vector.
    """

    if expected < 0 or dimensions < 1:
        raise ValueError("expected must be >= 0 and dimensions >= 1")
    vectors: list[Vector | None] = [None] * expected
    for index, raw in items:
        if not 0 <= index < expected:
            raise IntegrityError(f"Embedding index {index} outside 0..{expected - 1}")
        if vectors[index] is not None:
            raise IntegrityError(f"Duplicate embedding for index {index}")
        if len(raw) != dimensions:
            raise IntegrityError(
                f"Embedding {index} has {len(raw)} dimensions, expected {dimensions}"
            )
        vector = tuple(float(value) for value in raw)
        if not all(math.isfinite(value) for value in vector):
            raise IntegrityError(f"Embedding {index} contains non-finite values")
        if not any(vector):
            raise IntegrityError(f"Embedding {index} is an all-zero vector")
        vectors[index] = vector

    missing = [index for index, vector in enumerate(vectors) if vector is None]
    if missing:
        preview = ", ".join(str(index) for index in missing[:10])
        raise IntegrityError(f"Missing {len(missing)} embedding(s): {preview}")
    return [vector for vector in vectors if vector is not None]
