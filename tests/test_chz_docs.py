from datetime import date

import pytest

from chzkiz import chz_api, chz_docs

OTHER = {"type": "OTHER", "number": "848854273", "date": "2026-10-05", "custom_name": "Отчет о реализации"}


def test_doc_types_match_true_api():
    assert chz_api.DOC_TYPE_RECEIPT == "LK_RECEIPT"
    assert chz_api.DOC_TYPE_RETURN == "LP_RETURN"


def test_build_receipt_doc_fields():
    doc = chz_docs.build_receipt_doc("7712345678", [{"cis": "code1", "product_cost": 15000}])
    assert doc == {
        "inn": "7712345678",
        "action": "DISTANCE",
        "action_date": date.today().isoformat(),
        "products": [{"cis": "code1", "product_cost": 15000}],
    }


def test_build_receipt_doc_primary_other():
    doc = chz_docs.build_receipt_doc("7712345678", [{"cis": "c1", "product_cost": 100}], OTHER)
    assert doc["document_type"] == "OTHER"
    assert doc["document_number"] == "848854273"
    assert doc["document_date"] == "2026-10-05"
    assert doc["primary_document_custom_name"] == "Отчет о реализации"


def test_build_receipt_doc_receipt_has_no_custom_name():
    primary = {"type": "RECEIPT", "number": "5", "date": "2026-10-01"}
    doc = chz_docs.build_receipt_doc("7712345678", [{"cis": "c1", "product_cost": 100}], primary)
    assert doc["document_type"] == "RECEIPT"
    assert "primary_document_custom_name" not in doc


def test_build_return_doc_paid_carries_primary():
    doc = chz_docs.build_return_doc("7712345678", [{"cis": "code2", "paid": True}], OTHER)
    assert doc == {
        "trade_participant_inn": "7712345678",
        "return_type": "REMOTE_SALE_RETURN",
        "products_list": [{
            "ki": "code2",
            "paid": True,
            "primary_document_type": "OTHER",
            "primary_document_number": "848854273",
            "primary_document_date": "2026-10-05",
            "primary_document_custom_name": "Отчет о реализации",
        }],
    }


def test_build_return_doc_unpaid_has_no_primary():
    doc = chz_docs.build_return_doc("7712345678", [{"cis": "code3", "paid": False}], OTHER)
    assert doc["products_list"] == [{"ki": "code3", "paid": False}]


def test_build_return_doc_item_primary_wins():
    own = {"type": "RECEIPT", "number": "77", "date": "2026-10-02"}
    doc = chz_docs.build_return_doc(
        "7712345678", [{"cis": "code4", "paid": True, "primary_document": own}], OTHER
    )
    item = doc["products_list"][0]
    assert item["primary_document_type"] == "RECEIPT"
    assert item["primary_document_number"] == "77"
    assert "primary_document_custom_name" not in item


def test_build_return_doc_paid_without_primary_fails():
    with pytest.raises(ValueError):
        chz_docs.build_return_doc("7712345678", [{"cis": "code5", "paid": True}])
