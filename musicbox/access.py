"""访问控制：.env 里的静态白名单 + 管理员通过命令维护的动态白名单（存 SQLite）"""

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Access


class Kind(str, Enum):
    USER = 'user'
    CHAT = 'chat'

    @classmethod
    def of(cls, telegram_id: int) -> 'Kind':
        """Telegram 里用户 ID 为正数，群 / 频道 ID 为负数"""
        return cls.USER if telegram_id > 0 else cls.CHAT


class AllowStore:
    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute('''CREATE TABLE IF NOT EXISTS allowlist (
                                kind TEXT NOT NULL,
                                id INTEGER NOT NULL,
                                added_by INTEGER,
                                created_at INTEGER NOT NULL,
                                PRIMARY KEY (kind, id))''')
        self._db.commit()

    def add(self, kind: Kind, telegram_id: int, added_by: int | None) -> bool:
        cur = self._db.execute('INSERT OR IGNORE INTO allowlist VALUES (?, ?, ?, ?)',
                               (kind.value, telegram_id, added_by, int(time.time())))
        self._db.commit()
        return cur.rowcount > 0

    def remove(self, kind: Kind, telegram_id: int) -> bool:
        cur = self._db.execute('DELETE FROM allowlist WHERE kind = ? AND id = ?', (kind.value, telegram_id))
        self._db.commit()
        return cur.rowcount > 0

    def contains(self, kind: Kind, telegram_id: int) -> bool:
        return self._db.execute('SELECT 1 FROM allowlist WHERE kind = ? AND id = ?',
                                (kind.value, telegram_id)).fetchone() is not None

    def ids(self, kind: Kind) -> list[int]:
        rows = self._db.execute('SELECT id FROM allowlist WHERE kind = ? ORDER BY created_at', (kind.value,))
        return [row[0] for row in rows]

    def close(self):
        self._db.close()


class ChangeResult(Enum):
    DONE = 'done'
    UNCHANGED = 'unchanged'     # 本来就在 / 不在白名单
    STATIC = 'static'           # 在 .env 里配置的，命令改不了


@dataclass
class AccessControl:
    config: Access
    store: AllowStore

    def is_admin(self, user_id: int | None) -> bool:
        return user_id in self.config.admins

    def allows(self, user_id: int | None, chat_id: int | None) -> bool:
        if self.config.allows(user_id, chat_id) or self.is_admin(user_id):
            return True
        return (user_id is not None and self.store.contains(Kind.USER, user_id)) or \
               (chat_id is not None and self.store.contains(Kind.CHAT, chat_id))

    def _static(self, kind: Kind) -> frozenset[int]:
        return self.config.users if kind == Kind.USER else self.config.chats

    def add(self, telegram_id: int, added_by: int) -> ChangeResult:
        kind = Kind.of(telegram_id)
        if telegram_id in self._static(kind):
            return ChangeResult.UNCHANGED
        return ChangeResult.DONE if self.store.add(kind, telegram_id, added_by) else ChangeResult.UNCHANGED

    def remove(self, telegram_id: int) -> ChangeResult:
        kind = Kind.of(telegram_id)
        if telegram_id in self._static(kind):
            return ChangeResult.STATIC
        return ChangeResult.DONE if self.store.remove(kind, telegram_id) else ChangeResult.UNCHANGED

    def listing(self) -> dict[Kind, tuple[list[int], list[int]]]:
        """每类返回 (.env 中配置的, 命令添加的)"""
        return {kind: (sorted(self._static(kind)), self.store.ids(kind)) for kind in Kind}
