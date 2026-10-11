import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    # 访问网易云时使用的代理，例如 http://user:pass@host:port 或 socks5://host:port
    netease_proxy: str | None = None
    cache_db: Path = Path('data/cache.db')
    max_downloads: int = 5
    log_level: str = 'INFO'

    @classmethod
    def from_env(cls) -> 'Settings':
        token = os.environ.get('TG_BOT_TOKEN', '').strip()
        if not token:
            raise SystemExit('请设置环境变量 TG_BOT_TOKEN')
        return cls(token=token,
                   netease_proxy=os.environ.get('NETEASE_PROXY') or None,
                   cache_db=Path(os.environ.get('CACHE_DB', cls.cache_db)),
                   max_downloads=int(os.environ.get('MAX_DOWNLOADS', cls.max_downloads)),
                   log_level=os.environ.get('LOG_LEVEL', cls.log_level).upper())
