from chzkiz import chz_api, sverka

GTIN = "46031234567890"
OUR_INN = "7712345678"
OTHER_INN = "9998887766"


def code(serial: str) -> str:
    return f"01{GTIN}21{serial}"


def test_sale_introduced_goes_to_output():
    items = [{"cis_raw": code("1"), "op": "sale", "product_cost": 100}]
    info = {code("1"): {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_OUTPUT
    assert rows[0]["reason"] == "sale_in_turnover"


def test_return_retired_goes_to_return():
    items = [{"cis_raw": code("2"), "op": "return", "product_cost": 100}]
    info = {code("2"): {"status": chz_api.STATUS_RETIRED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_RETURN
    assert rows[0]["reason"] == "return_retired"


def test_sale_already_retired_is_ok():
    items = [{"cis_raw": code("3"), "op": "sale", "product_cost": 100}]
    info = {code("3"): {"status": chz_api.STATUS_RETIRED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_OK
    assert rows[0]["reason"] == "already_output"


def test_return_already_introduced_is_ok():
    items = [{"cis_raw": code("4"), "op": "return", "product_cost": 100}]
    info = {code("4"): {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_OK
    assert rows[0]["reason"] == "already_in_turnover"


def test_not_found_in_chz_is_error():
    items = [{"cis_raw": code("5"), "op": "sale", "product_cost": 100}]
    rows = sverka.classify_items(items, {}, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_ERROR
    assert rows[0]["reason"] == "not_found"


def test_wrong_owner_is_error():
    items = [{"cis_raw": code("6"), "op": "sale", "product_cost": 100}]
    info = {code("6"): {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OTHER_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_ERROR
    assert rows[0]["reason"] == "wrong_owner"


def test_foreign_owner_only_blocks_needed_action():
    cases = [
        ("sale", chz_api.STATUS_RETIRED, sverka.GROUP_OK, "already_output"),
        ("return", chz_api.STATUS_INTRODUCED, sverka.GROUP_OK, "already_in_turnover"),
        ("return", chz_api.STATUS_RETIRED, sverka.GROUP_ERROR, "wrong_owner"),
        ("sale", "EMITTED", sverka.GROUP_ERROR, "wrong_owner"),
    ]
    for i, (op, status, group, reason) in enumerate(cases):
        c = code(f"9{i}")
        items = [{"cis_raw": c, "op": op, "product_cost": 100}]
        info = {c: {"status": status, "ownerInn": OTHER_INN}}
        rows = sverka.classify_items(items, info, OUR_INN)
        assert (rows[0]["group"], rows[0]["reason"]) == (group, reason), (op, status)


def test_not_introduced_status_is_error():
    items = [{"cis_raw": code("7"), "op": "sale", "product_cost": 100}]
    info = {code("7"): {"status": chz_api.STATUS_EMITTED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_ERROR
    assert rows[0]["reason"] == "not_introduced"


def test_bad_format_is_error():
    items = [{"cis_raw": "не_код_вообще", "op": "sale", "product_cost": 100}]
    rows = sverka.classify_items(items, {}, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_ERROR
    assert rows[0]["reason"] == "bad_format"


def test_duplicate_code_in_same_run_is_error():
    c = code("8")
    items = [
        {"cis_raw": c, "op": "sale", "product_cost": 100},
        {"cis_raw": c, "op": "sale", "product_cost": 100},
    ]
    info = {c: {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_OUTPUT
    assert rows[1]["group"] == sverka.GROUP_ERROR
    assert rows[1]["reason"] == "duplicate"


def test_counts_by_group():
    items = [
        {"cis_raw": code("9"), "op": "sale", "product_cost": 1},
        {"cis_raw": code("10"), "op": "return", "product_cost": 1},
    ]
    info = {
        code("9"): {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OUR_INN},
        code("10"): {"status": chz_api.STATUS_RETIRED, "ownerInn": OUR_INN},
    }
    rows = sverka.classify_items(items, info, OUR_INN)
    counts = sverka.counts_by_group(rows)
    assert counts[sverka.GROUP_OUTPUT] == 1
    assert counts[sverka.GROUP_RETURN] == 1
    assert counts[sverka.GROUP_ERROR] == 0


def test_sale_canceled_by_wb_is_error_not_output():
    items = [{"cis_raw": code("7"), "op": "sale", "product_cost": 100, "reason": "wb_canceled"}]
    info = {code("7"): {"status": chz_api.STATUS_INTRODUCED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert rows[0]["group"] == sverka.GROUP_ERROR
    assert rows[0]["reason"] == "wb_canceled"
    assert rows[0]["chz_status"] == chz_api.STATUS_INTRODUCED


def test_later_return_wins_over_wb_canceled_sale():
    items = [
        {"cis_raw": code("8"), "op": "sale", "product_cost": 100, "date": "2026-09-30", "reason": "wb_canceled"},
        {"cis_raw": code("8"), "op": "return", "product_cost": 100, "date": "2026-10-02"},
    ]
    info = {code("8"): {"status": chz_api.STATUS_RETIRED, "ownerInn": OUR_INN}}
    rows = sverka.classify_items(items, info, OUR_INN)
    assert len(rows) == 1
    assert rows[0]["group"] == sverka.GROUP_RETURN
