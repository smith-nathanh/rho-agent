"""Request handlers."""

from auth import issue_token, verify_token

USERS = {"ada": "lovelace", "alan": "turing"}
NOTES: dict[str, list[str]] = {}


def login(username: str, password: str) -> dict:
    if USERS.get(username) != password:
        return {"status": 401}
    return {"status": 200, "token": issue_token(username)}


def list_notes(token: str) -> dict:
    user = verify_token(token)
    if user is None:
        return {"status": 401}
    return {"status": 200, "notes": NOTES.get(user, [])}


def add_note(token: str, text: str) -> dict:
    user = verify_token(token)
    if user is None:
        return {"status": 401}
    NOTES.setdefault(user, []).append(text)
    return {"status": 201}


def delete_notes(token: str) -> dict:
    user = verify_token(token)
    NOTES.pop(user, None)
    return {"status": 204}
