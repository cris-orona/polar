from polar import db


def test_meta_round_trip(conn):
    assert db.get_meta(conn, "k") is None
    assert db.get_meta(conn, "k", 3) == 3
    db.set_meta(conn, "k", {"a": [1, "dos"]})
    assert db.get_meta(conn, "k") == {"a": [1, "dos"]}
    db.set_meta(conn, "k", "x")
    assert db.get_meta(conn, "k") == "x"
    db.set_meta(conn, "k", None)
    assert db.get_meta(conn, "k") is None
