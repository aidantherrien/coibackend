from decimal import Decimal

import pytest

from coi_backend.mapping import MappingNeedsReview, map_invoice, map_oa


def invoice_payload() -> dict[str, object]:
    return {
        "invoice": {
            "invoiceNo": " 317045 ",
            "poNo": " jf823-09 ",
            "orderNo": "2507177",
            "invoiceDate": "2026/07/01",
        },
        "customField": {
            "salesman": "0053",
            "currency": "usd",
            "orderDate": "2026/06/15",
            "freight": "PREPAID",
        },
        "paymentDetails": {
            "subtotal": "100.00",
            "tax": "6.00",
            "total": "106.00",
            "paymentTerms": "NET 30",
        },
        "lineItems": [
            {
                "lineNo": "001",
                "ordQty": "1",
                "shipQty": "1",
                "productCode": "ABC",
                "discounts": "10%",
                "netPrice": "100.000",
                "extension": "100.00",
            }
        ],
    }


def test_invoice_mapping_preserves_exact_values_and_fixed_fallback() -> None:
    record = map_invoice(invoice_payload(), vendor="ARTOPEX")
    assert record.invoice_no == "317045"
    assert record.po == "JF823-09"
    assert record.salesman == "0053"
    assert record.total == Decimal("106.00")
    assert record.currency == "USD"
    assert record.lines[0].line_position == 1
    assert record.lines[0].source_line_no == "001"
    assert record.lines[0].discount_pct == Decimal("10")


def test_zero_total_is_a_present_required_value() -> None:
    payload = invoice_payload()
    payload["paymentDetails"]["total"] = "0.00"  # type: ignore[index]
    assert map_invoice(payload, vendor="ARTOPEX").total == Decimal("0.00")


def test_missing_required_business_key_becomes_review() -> None:
    payload = invoice_payload()
    payload["invoice"]["poNo"] = " "  # type: ignore[index]
    with pytest.raises(MappingNeedsReview, match="po"):
        map_invoice(payload, vendor="ARTOPEX")


def test_currency_must_match_the_database_ascii_constraint() -> None:
    payload = invoice_payload()
    payload["customField"]["currency"] = "ÜSD"  # type: ignore[index]
    with pytest.raises(MappingNeedsReview, match="currency"):
        map_invoice(payload, vendor="ARTOPEX")


def test_null_primary_alias_uses_populated_fallback() -> None:
    payload = invoice_payload()
    line = payload["lineItems"][0]  # type: ignore[index]
    line["discountPct"] = None
    line["discounts"] = "12.5%"
    line["productCode"] = "abc-lower"
    record = map_invoice(payload, vendor="ARTOPEX")
    assert record.lines[0].discount_pct == Decimal("12.5")
    assert record.lines[0].product_code == "ABC-LOWER"


def test_oa_mapping_uses_parser_invoice_fields_but_keeps_oa_shape() -> None:
    payload = {
        "invoice": {
            "invoiceNo": "2606287",
            "poNo": "JF1069-05",
            "invoiceDate": "2026/05/07",
            "deliveryDate": "2026/06/01",
        },
        "customField": {"salesman": "5053", "currency": "USD"},
        "paymentDetails": {"subtotal": "90", "total": "90"},
        "lineItems": [
            {
                "line": "900",
                "qty": "0",
                "productCode": "ACC",
                "netPrice": "0.770",
                "extension": "0.00",
            }
        ],
    }
    record = map_oa(payload, vendor="ARTOPEX")
    assert record.order_no == "2606287"
    assert record.lines[0].source_line_no == "900"
    assert record.lines[0].qty == Decimal("0")


def test_oa_null_line_alias_uses_line_number_fallback() -> None:
    payload = {
        "invoice": {"invoiceNo": "OA-2", "poNo": "PO-2"},
        "paymentDetails": {"total": "0"},
        "lineItems": [{"line": None, "lineNo": "007"}],
    }
    assert map_oa(payload, vendor="ARTOPEX").lines[0].source_line_no == "007"
