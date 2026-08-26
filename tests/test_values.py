from datetime import date
from decimal import Decimal

import pytest

from coi_backend.values import (
    nested_value,
    normalize_identifier,
    parse_date,
    parse_decimal,
    parse_integer,
    parse_percentage,
)


def test_nested_value_and_identifier_normalization() -> None:
    payload = {"invoice": {"poNo": "  jf123-01  "}}
    assert nested_value(payload, "invoice.poNo") == "  jf123-01  "
    assert nested_value(payload, "invoice.missing") is None
    assert normalize_identifier(payload["invoice"]["poNo"], uppercase=True) == "JF123-01"
    assert normalize_identifier("  ") is None
    with pytest.raises(ValueError, match="scalar"):
        normalize_identifier({"unexpected": "object"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2,410.00", Decimal("2410.00")),
        ("$ 20.404", Decimal("20.404")),
        ("USD 0.10", Decimal("0.10")),
        ("-$1.25", Decimal("-1.25")),
        ("USD -1.25", Decimal("-1.25")),
        ("(42.50)", Decimal("-42.50")),
        (0, Decimal("0")),
        (None, None),
    ],
)
def test_parse_decimal_is_exact(raw: object, expected: Decimal | None) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["12abc34", "1,23.00", "($1.00", "($-1.00)", True, "1.2.3"])
def test_parse_decimal_rejects_ambiguous_values(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_decimal(raw)


def test_integer_and_percentage_parsers_are_explicit() -> None:
    assert parse_integer("900") == 900
    assert parse_percentage("61.500%") == Decimal("61.500")
    with pytest.raises(ValueError):
        parse_integer("1.5")


def test_decimal_parser_rejects_database_rounding_and_overflow() -> None:
    assert parse_decimal("1.000", max_decimal_places=2) == Decimal("1.000")
    with pytest.raises(ValueError, match="decimal places"):
        parse_decimal("1.005", max_decimal_places=2)
    with pytest.raises(ValueError, match="integer digits"):
        parse_decimal("1234", max_integer_digits=3)
    with pytest.raises(ValueError, match="finite"):
        parse_decimal(Decimal("NaN"))


def test_parse_date_validates_calendar_dates() -> None:
    assert parse_date("2026/08/25") == date(2026, 8, 25)
    assert parse_date("08/25/2026") == date(2026, 8, 25)
    with pytest.raises(ValueError):
        parse_date("2026-02-30")
