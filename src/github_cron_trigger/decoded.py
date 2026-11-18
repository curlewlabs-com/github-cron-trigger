"""Narrowing for values read out of decoded JSON, and nothing else.

pyright types the result of `isinstance(value, dict)`, `list` or `Mapping` on a
decoded value as a container of unknown contents, which strict mode refuses to
let through. Each function here makes that one check and returns the value under
a type that names what a reader of it may assume, so the check is written once
and every caller reads a typed result. A function returns None for a value of
another shape, and never raises.
"""

from collections.abc import Mapping
from typing import Any, cast


def as_mapping(value: object) -> Mapping[object, object] | None:
    """`value` when it is a mapping of any kind."""
    return cast(Mapping[object, object], value) if isinstance(value, Mapping) else None


def as_dict(value: object) -> dict[str, Any] | None:
    """`value` when it is a dict, as JSON decoding makes one: with string keys."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def as_list(value: object) -> list[Any] | None:
    """A copy of `value` when it is a list."""
    if not isinstance(value, list):
        return None
    items: list[Any] = [*value]
    return items


def as_str_list(value: object) -> list[str] | None:
    """A copy of `value` when it is a list holding only strings."""
    items = as_list(value)
    if items is None or not all(isinstance(item, str) for item in items):
        return None
    strings: list[str] = [*items]
    return strings
