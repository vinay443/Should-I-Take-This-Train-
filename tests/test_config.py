import pytest

from sitt.config import parse_allowed_user_ids


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, set()),
        ("", set()),
        (" , ", set()),
        ("123", {123}),
        ("123,456", {123, 456}),
        (" 123 , 456 ", {123, 456}),
        ("123 456\n789", {123, 456, 789}),
    ],
)
def test_parse_allowed_user_ids(raw, expected):
    assert parse_allowed_user_ids(raw) == frozenset(expected)


@pytest.mark.parametrize("raw", ["abc", "123,@me", "-5", "1.5"])
def test_parse_allowed_user_ids_rejects_non_ids(raw):
    with pytest.raises(ValueError, match="not a Telegram user ID"):
        parse_allowed_user_ids(raw)
