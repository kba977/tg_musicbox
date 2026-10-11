"""file_id 持久化缓存，以及并发控制用的锁"""

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class FileIdCache:
    """歌曲上传到 Telegram 后记下 file_id，之后同一首歌直接转发，不再下载上传"""

    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute('''CREATE TABLE IF NOT EXISTS file_ids (
                                song_id INTEGER PRIMARY KEY,
                                file_id TEXT NOT NULL,
                                caption TEXT NOT NULL DEFAULT '',
                                created_at INTEGER NOT NULL)''')
        self._db.commit()

    def get(self, song_id: int) -> tuple[str, str] | None:
        row = self._db.execute('SELECT file_id, caption FROM file_ids WHERE song_id = ?',
                               (song_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def set(self, song_id: int, file_id: str, caption: str):
        self._db.execute('INSERT OR REPLACE INTO file_ids VALUES (?, ?, ?, ?)',
                         (song_id, file_id, caption, int(time.time())))
        self._db.commit()

    def delete(self, song_id: int):
        self._db.execute('DELETE FROM file_ids WHERE song_id = ?', (song_id,))
        self._db.commit()

    def close(self):
        self._db.close()


class KeyedLock:
    """按 key 加锁，没人使用的锁会被自动清理"""

    def __init__(self):
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    def __len__(self):
        return len(self._locks)

    @asynccontextmanager
    async def __call__(self, key: int) -> AsyncIterator[None]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


class BusySet:
    """同一个 key 同一时间只允许一个任务，进入失败返回 False"""

    def __init__(self):
        self._busy: set[int] = set()

    def __len__(self):
        return len(self._busy)

    @asynccontextmanager
    async def __call__(self, key: int) -> AsyncIterator[bool]:
        if key in self._busy:
            yield False
            return
        self._busy.add(key)
        try:
            yield True
        finally:
            self._busy.discard(key)
