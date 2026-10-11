import pytest

from musicbox.config import Access, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ('TG_BOT_TOKEN', 'PUBLIC_ACCESS', 'ALLOWED_USERS', 'ALLOWED_CHATS', 'NETEASE_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TG_BOT_TOKEN', 'token')


def test_defaults_to_private_with_empty_whitelist():
    access = Settings.from_env().access
    assert access == Access(public=False, users=frozenset(), chats=frozenset())
    assert not access.allows(1, 1)


@pytest.mark.parametrize('value, expected', [('true', True), ('TRUE', True), ('1', True), ('yes', True),
                                             ('false', False), ('0', False), ('', False)])
def test_public_access(monkeypatch, value, expected):
    monkeypatch.setenv('PUBLIC_ACCESS', value)
    assert Settings.from_env().access.public is expected


def test_invalid_public_access(monkeypatch):
    monkeypatch.setenv('PUBLIC_ACCESS', 'maybe')
    with pytest.raises(SystemExit, match='PUBLIC_ACCESS'):
        Settings.from_env()


def test_allowed_ids(monkeypatch):
    monkeypatch.setenv('ALLOWED_USERS', ' 123, 456 ,')
    monkeypatch.setenv('ALLOWED_CHATS', '-1001234567890')
    access = Settings.from_env().access
    assert access.users == {123, 456} and access.chats == {-1001234567890}
    assert access.allows(123, 999) and access.allows(999, -1001234567890) and not access.allows(999, 999)


def test_invalid_allowed_ids(monkeypatch):
    monkeypatch.setenv('ALLOWED_USERS', '123,@someone')
    with pytest.raises(SystemExit, match='ALLOWED_USERS'):
        Settings.from_env()


def test_public_allows_everyone():
    assert Access(public=True).allows(None, None)


def test_missing_token(monkeypatch):
    monkeypatch.delenv('TG_BOT_TOKEN')
    with pytest.raises(SystemExit, match='TG_BOT_TOKEN'):
        Settings.from_env()
