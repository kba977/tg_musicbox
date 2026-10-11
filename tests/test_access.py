from musicbox.access import AccessControl, AllowStore, ChangeResult, Kind
from musicbox.config import Access


def test_kind_of():
    assert Kind.of(123) == Kind.USER
    assert Kind.of(-1001234567890) == Kind.CHAT


def test_allow_store_persists(tmp_path):
    path = tmp_path / 'data' / 'cache.db'
    store = AllowStore(path)
    assert store.add(Kind.USER, 7, added_by=1)
    assert not store.add(Kind.USER, 7, added_by=1)
    store.close()

    store = AllowStore(path)
    assert store.contains(Kind.USER, 7) and not store.contains(Kind.CHAT, 7)
    assert store.ids(Kind.USER) == [7]
    assert store.remove(Kind.USER, 7) and not store.remove(Kind.USER, 7)
    store.close()


def test_access_control_combines_static_and_dynamic():
    access = AccessControl(Access(users=frozenset({42}), admins=frozenset({1})), AllowStore(':memory:'))
    assert access.allows(42, None) and access.allows(1, None) and not access.allows(7, None)

    assert access.add(7, added_by=1) == ChangeResult.DONE
    assert access.add(42, added_by=1) == ChangeResult.UNCHANGED
    assert access.allows(7, None)
    assert access.listing() == {Kind.USER: ([42], [7]), Kind.CHAT: ([], [])}

    assert access.remove(42) == ChangeResult.STATIC
    assert access.remove(7) == ChangeResult.DONE
    assert access.remove(7) == ChangeResult.UNCHANGED
    assert not access.allows(7, None)
