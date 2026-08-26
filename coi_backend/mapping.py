"""Map PDF.co's vendor-shaped JSON into typed PostgreSQL records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .values import (
    nested_value,
    normalize_identifier,
    parse_date,
    parse_decimal,
    parse_percentage,
)


class MappingNeedsReview(ValueError):
    """Raised when a parse is valid JSON but lacks required business fields."""


@dataclass(frozen=True)
class InvoiceLine:
    line_position: int
    source_line_no: str | None
    ord_qty: Decimal | None
    ship_qty: Decimal | None
    bo_qty: Decimal | None
    product_code: str | None
    description: str | None
    price_list: Decimal | None
    discount_pct: Decimal | None
    net_price: Decimal | None
    extension: Decimal | None


@dataclass(frozen=True)
class InvoiceRecord:
    vendor: str
    invoice_no: str
    order_no: str | None
    po: str
    account_no: str | None
    salesman: str | None
    invoice_date: date | None
    order_date: date | None
    terms: str | None
    freight_terms: str | None
    subtotal: Decimal | None
    freight: Decimal | None
    misc: Decimal | None
    tax: Decimal | None
    less_prepaid_deposit: Decimal | None
    total: Decimal
    currency: str
    lines: tuple[InvoiceLine, ...]


@dataclass(frozen=True)
class OaLine:
    line_position: int
    source_line_no: str | None
    qty: Decimal | None
    product_code: str | None
    description: str | None
    retail_price: Decimal | None
    retail_extension: Decimal | None
    discount_pct: Decimal | None
    net_price: Decimal | None
    extension: Decimal | None


@dataclass(frozen=True)
class OaRecord:
    vendor: str
    order_no: str
    po: str
    account_no: str | None
    salesman: str | None
    order_date: date | None
    ship_date: date | None
    terms: str | None
    reference: str | None
    freight_terms: str | None
    fob: str | None
    subtotal: Decimal | None
    freight: Decimal | None
    total: Decimal
    retail_extension_total: Decimal | None
    currency: str
    lines: tuple[OaLine, ...]


def _first(node: dict[str, Any], paths: Iterable[str]) -> Any:
    for path in paths:
        value = nested_value(node, path)
        if value is not None:
            return value
    return None


def _text(value: Any, *, uppercase: bool = False) -> str | None:
    return normalize_identifier(value, uppercase=uppercase)


def _currency(value: Any) -> str:
    currency = _text("USD" if value is None else value, uppercase=True)
    if currency is None or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise MappingNeedsReview(f"invalid currency code: {value!r}")
    return currency


def _money(value: Any) -> Decimal | None:
    return parse_decimal(value, max_decimal_places=2, max_integer_digits=16)


def _quantity(value: Any) -> Decimal | None:
    return parse_decimal(value, max_decimal_places=3, max_integer_digits=15)


def _unit_price(value: Any) -> Decimal | None:
    return parse_decimal(value, max_decimal_places=4, max_integer_digits=14)


def _discount(value: Any) -> Decimal | None:
    return parse_percentage(value, max_decimal_places=4, max_integer_digits=5)


def _line_items(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    items = parsed.get("lineItems", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise MappingNeedsReview("lineItems must be an array of objects")
    return items


def _require(values: dict[str, Any]) -> None:
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise MappingNeedsReview("missing required fields: " + ", ".join(sorted(missing)))


def map_invoice(parsed: dict[str, Any], *, vendor: str) -> InvoiceRecord:
    invoice_no = _text(nested_value(parsed, "invoice.invoiceNo"))
    po = _text(nested_value(parsed, "invoice.poNo"), uppercase=True)
    total = _money(nested_value(parsed, "paymentDetails.total"))
    _require({"invoice_no": invoice_no, "po": po, "total": total})

    lines = tuple(
        InvoiceLine(
            line_position=position,
            source_line_no=_text(item.get("lineNo")),
            ord_qty=_quantity(item.get("ordQty")),
            ship_qty=_quantity(item.get("shipQty")),
            bo_qty=_quantity(item.get("boQty")),
            product_code=_text(item.get("productCode"), uppercase=True),
            description=_text(item.get("description")),
            price_list=_unit_price(item.get("priceList")),
            discount_pct=_discount(_first(item, ("discountPct", "discounts"))),
            net_price=_unit_price(item.get("netPrice")),
            extension=_money(item.get("extension")),
        )
        for position, item in enumerate(_line_items(parsed), start=1)
    )

    assert invoice_no is not None and po is not None and total is not None
    return InvoiceRecord(
        vendor=vendor,
        invoice_no=invoice_no,
        order_no=_text(nested_value(parsed, "invoice.orderNo")),
        po=po,
        account_no=_text(nested_value(parsed, "customField.accountNo")),
        salesman=_text(_first(parsed, ("customField.slm", "customField.salesman"))),
        invoice_date=parse_date(nested_value(parsed, "invoice.invoiceDate")),
        order_date=parse_date(nested_value(parsed, "customField.orderDate")),
        terms=_text(nested_value(parsed, "paymentDetails.paymentTerms")),
        freight_terms=_text(nested_value(parsed, "customField.freight")),
        subtotal=_money(nested_value(parsed, "paymentDetails.subtotal")),
        freight=_money(_first(parsed, ("paymentDetails.freight", "paymentDetails.shipping"))),
        misc=_money(_first(parsed, ("paymentDetails.misc", "paymentDetails.miscellaneous"))),
        tax=_money(_first(parsed, ("paymentDetails.tax", "paymentDetails.taxTotal"))),
        less_prepaid_deposit=_money(nested_value(parsed, "customField.lessPrepaidDeposit")),
        total=total,
        currency=_currency(_first(parsed, ("customField.currency", "paymentDetails.currency"))),
        lines=lines,
    )


def map_oa(parsed: dict[str, Any], *, vendor: str) -> OaRecord:
    # PDF.co's invoice parser emits the OA order number under invoiceNo.
    order_no = _text(nested_value(parsed, "invoice.invoiceNo"))
    po = _text(nested_value(parsed, "invoice.poNo"), uppercase=True)
    total = _money(nested_value(parsed, "paymentDetails.total"))
    _require({"order_no": order_no, "po": po, "total": total})

    lines = tuple(
        OaLine(
            line_position=position,
            source_line_no=_text(_first(item, ("line", "lineNo"))),
            qty=_quantity(item.get("qty")),
            product_code=_text(item.get("productCode"), uppercase=True),
            description=_text(item.get("description")),
            retail_price=_unit_price(item.get("retailPrice")),
            retail_extension=_money(item.get("retailExtension")),
            discount_pct=_discount(_first(item, ("discountPct", "discount"))),
            net_price=_unit_price(item.get("netPrice")),
            extension=_money(item.get("extension")),
        )
        for position, item in enumerate(_line_items(parsed), start=1)
    )

    assert order_no is not None and po is not None and total is not None
    return OaRecord(
        vendor=vendor,
        order_no=order_no,
        po=po,
        account_no=_text(nested_value(parsed, "customField.accountNo")),
        salesman=_text(_first(parsed, ("customField.salesman", "customField.slm"))),
        order_date=parse_date(nested_value(parsed, "invoice.invoiceDate")),
        ship_date=parse_date(nested_value(parsed, "invoice.deliveryDate")),
        terms=_text(nested_value(parsed, "paymentDetails.paymentTerms")),
        reference=_text(_first(parsed, ("customField.referenceNo", "customField.reference"))),
        freight_terms=_text(nested_value(parsed, "customField.freight")),
        fob=_text(nested_value(parsed, "customField.fob")),
        subtotal=_money(nested_value(parsed, "paymentDetails.subtotal")),
        freight=_money(_first(parsed, ("paymentDetails.freight", "paymentDetails.shipping"))),
        total=total,
        retail_extension_total=_money(nested_value(parsed, "customField.retailExtensionTotal")),
        currency=_currency(_first(parsed, ("customField.currency", "paymentDetails.currency"))),
        lines=lines,
    )
