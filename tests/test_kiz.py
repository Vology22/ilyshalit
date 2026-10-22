from chzkiz import kiz

GTIN = "46031234567890"  # 14 цифр


def test_normalize_strip_gs_byte():
    raw = f"01{GTIN}21serial123\x1d91EE01\x1d92AbCdEf=="
    assert kiz.normalize_kiz(raw) == f"01{GTIN}21serial123"


def test_normalize_strip_literal_u001d():
    raw = f"01{GTIN}21serial123\\u001d91EE01\\u001d92AbCdEf=="
    assert kiz.normalize_kiz(raw) == f"01{GTIN}21serial123"


def test_normalize_literal_u001d_case_insensitive():
    raw = f"01{GTIN}21serial123\\U001D91EE01"
    assert kiz.normalize_kiz(raw) == f"01{GTIN}21serial123"


def test_normalize_no_gs_passthrough():
    raw = "просто_строка_без_gs"
    assert kiz.normalize_kiz(raw) == raw


def test_normalize_no_tail_keeps_full_code():
    raw = f"01{GTIN}21serial123"
    assert kiz.normalize_kiz(raw) == raw


def test_is_well_formed_true():
    assert kiz.is_well_formed(f"01{GTIN}21serial123")


def test_is_well_formed_bad_prefix():
    assert not kiz.is_well_formed(f"99{GTIN}21serial123")


def test_is_well_formed_short_gtin():
    assert not kiz.is_well_formed("0146031234")


def test_is_well_formed_missing_serial_marker():
    assert not kiz.is_well_formed(f"01{GTIN}noserial")
