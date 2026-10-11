import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Access:
    """.env 里配置的访问规则：公开时所有人可用，否则只有白名单里的用户 / 群"""
    public: bool = False
    users: frozenset[int] = frozenset()
    chats: frozenset[int] = frozenset()
    admins: frozenset[int] = frozenset()   # 可以用 /allow /deny 管理白名单

    def allows(self, user_id: int | None, chat_id: int | None) -> bool:
        return self.public or user_id in self.users or chat_id in self.chats


@dataclass(frozen=True)
class Settings:
    token: str
    # 访问网易云时使用的代理，例如 http://user:pass@host:port 或 socks5://host:port
    netease_proxy: str | None = None
    cache_db: Path = Path('data/cache.db')
    max_downloads: int = 5
    log_level: str = 'INFO'
    access: Access = field(default_factory=Access)

    @classmethod
    def from_env(cls) -> 'Settings':
        token = os.environ.get('TG_BOT_TOKEN', '').strip()
        if not token:
            raise SystemExit('请设置环境变量 TG_BOT_TOKEN')
        return cls(token=token,
                   netease_proxy=os.environ.get('NETEASE_PROXY') or None,
                   cache_db=Path(os.environ.get('CACHE_DB', cls.cache_db)),
                   max_downloads=int(os.environ.get('MAX_DOWNLOADS', cls.max_downloads)),
                   log_level=os.environ.get('LOG_LEVEL', cls.log_level).upper(),
                   access=Access(public=parse_bool('PUBLIC_ACCESS'),
                                 users=parse_ids('ALLOWED_USERS'),
                                 chats=parse_ids('ALLOWED_CHATS'),
                                 admins=parse_ids('ADMIN_USERS')))


def parse_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, '').strip().lower()
    if not value:
        return default
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    raise SystemExit(f'{name} 只能是 true 或 false，当前为 {value!r}')


def parse_ids(name: str) -> frozenset[int]:
    """逗号分隔的 Telegram ID 列表，例如 123456,-1001234567890"""
    items = [s.strip() for s in os.environ.get(name, '').split(',') if s.strip()]
    try:
        return frozenset(int(s) for s in items)
    except ValueError:
        raise SystemExit(f'{name} 应为逗号分隔的数字 ID，当前为 {os.environ[name]!r}') from None
