from routes import add_note, list_notes, login


def test_login_rejects_bad_password():
    assert login("ada", "wrong")["status"] == 401


def test_notes_round_trip():
    token = login("ada", "lovelace")["token"]
    assert add_note(token, "hello")["status"] == 201
    assert list_notes(token)["notes"] == ["hello"]
