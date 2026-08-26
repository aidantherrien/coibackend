"""Strict conversion helpers for parser output."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def nested_value(node: Any, path: str, default: Any = None) -> Any:
    """Walk a dotted path through dictionaries and normalize empty values."""

    current = node
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    if current is None:
        return default
    if isinstance(current, str) and not current.strip():
        return default
    return current


def normalize_identifier(value: Any, *, uppercase: bool = False) -> str | None:
    """Trim a business identifier and reject blank/control-character values."""

    if value is None:
        return None
    if isinstance(value, (bool, dict, list, tuple, set)):
        raise ValueError("identifier must be a scalar value")
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        return None
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("identifier contains control characters")
    return normalized.upper() if uppercase else normalized


def parse_decimal(
    value: Any,
    *,
    max_decimal_places: int | None = None,
    max_integer_digits: int | None = None,
) -> Decimal | None:
    """Parse an exact decimal without silently deleting unexpected characters."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, bool):
        raise ValueError("boolean is not a numeric value")
    else:
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, float):
            value = str(value)

        text = str(value).strip()
        negative_parentheses = text.startswith("(") and text.endswith(")")
        if text.startswith("(") != text.endswith(")"):
            raise ValueError(f"invalid accounting number: {value!r}")
        if negative_parentheses:
            text = text[1:-1].strip()

        sign = ""
        if text.startswith(("+", "-")):
            sign, text = text[0], text[1:].strip()

        for prefix in ("USD", "CAD", "$"):
            if text.upper().startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if text.startswith(("+", "-")):
            if sign:
                raise ValueError(f"invalid decimal value: {value!r}")
            sign, text = text[0], text[1:].strip()

        if negative_parentheses and sign:
            raise ValueError(f"invalid accounting number: {value!r}")
        text = sign + text

        if not _DECIMAL_PATTERN.fullmatch(text):
            raise ValueError(f"invalid decimal value: {value!r}")
        try:
            number = Decimal(text.replace(",", ""))
        except InvalidOperation as exc:  # pragma: no cover - guarded by regex
            raise ValueError(f"invalid decimal value: {value!r}") from exc
        if negative_parentheses:
            number = -number

    if not number.is_finite():
        raise ValueError(f"numeric value must be finite: {value!r}")
    if max_decimal_places is not None:
        quantum = Decimal(1).scaleb(-max_decimal_places)
        try:
            if number != number.quantize(quantum):
                raise ValueError(
                    f"numeric value has more than {max_decimal_places} decimal places: {value!r}"
                )
        except InvalidOperation as exc:
            raise ValueError(f"numeric value is outside the supported range: {value!r}") from exc
    if max_integer_digits is not None:
        integer_digits = max(number.copy_abs().adjusted() + 1, 0) if number else 0
        if integer_digits > max_integer_digits:
            raise ValueError(
                f"numeric value has more than {max_integer_digits} integer digits: {value!r}"
            )
    return number


def parse_integer(value: Any) -> int | None:
    number = parse_decimal(value)
    if number is None:
        return None
    integral = number.to_integral_value()
    if number != integral:
        raise ValueError(f"expected an integer, got {value!r}")
    return int(integral)


def parse_percentage(
    value: Any,
    *,
    max_decimal_places: int | None = None,
    max_integer_digits: int | None = None,
) -> Decimal | None:
    if isinstance(value, str):
        value = value.strip()
        if value.endswith("%"):
            value = value[:-1].strip()
    return parse_decimal(
        value,
        max_decimal_places=max_decimal_places,
        max_integer_digits=max_integer_digits,
    )


def parse_date(value: Any) -> date | None:
    """Parse only explicitly supported, real calendar dates."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported or invalid date: {value!r}")
