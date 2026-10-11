import asyncio

from musicbox.cache import BusySet, FileIdCache, KeyedLock


def test_file_id_cache_persists(tmp_path):
    path = tmp_path / 'sub' / 'cache.db'
    cache = FileIdCache(path)
    assert cache.get(1) is None
    cache.set(1, 'fid', '歌词')
    cache.close()

    cache = FileIdCache(path)
    assert cache.get(1) == ('fid', '歌词')
    cache.delete(1)
    assert cache.get(1) is None
    cache.close()


async def test_keyed_lock_serializes_and_cleans_up():
    lock = KeyedLock()
    running = []
    max_running = 0

    async def work(key):
        nonlocal max_running
        async with lock(key):
            running.append(key)
            max_running = max(max_running, running.count(1))
            await asyncio.sleep(0.01)
            running.remove(key)

    await asyncio.gather(*(work(1) for _ in range(5)), work(2))
    assert max_running == 1
    assert len(lock) == 0


async def test_busy_set():
    busy = BusySet()
    async with busy(1) as first:
        async with busy(1) as second:
            assert first and not second
        async with busy(2) as other:
            assert other
    assert len(busy) == 0
