from chzkiz import db


def test_run_isolated_between_users():
    with db.connect() as conn:
        user_a = db.get_or_create_user(conn, 201)
        user_b = db.get_or_create_user(conn, 202)
        run_id = db.create_run(conn, user_a["id"], "api")
        assert db.get_run(conn, run_id, user_a["id"]) is not None
        assert db.get_run(conn, run_id, user_b["id"]) is None


def test_items_scoped_to_run():
    with db.connect() as conn:
        user_a = db.get_or_create_user(conn, 203)
        run1 = db.create_run(conn, user_a["id"], "api")
        run2 = db.create_run(conn, user_a["id"], "api")
        db.add_items(
            conn,
            run1,
            [
                {
                    "cis_raw": "x",
                    "cis_norm": "x",
                    "op": "sale",
                    "group": "output",
                    "reason": "sale_in_turnover",
                    "product_cost": 1,
                }
            ],
        )
        assert len(db.get_items(conn, run1)) == 1
        assert len(db.get_items(conn, run2)) == 0


def test_wb_key_stored_encrypted_not_readable_without_decrypt():
    from chzkiz import crypto

    with db.connect() as conn:
        user = db.get_or_create_user(conn, 204)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("моя_секретная_строка"))
        row = db.get_user(conn, user["id"])
    assert row["wb_key_enc"] != "моя_секретная_строка"
    assert crypto.decrypt_str(row["wb_key_enc"]) == "моя_секретная_строка"
