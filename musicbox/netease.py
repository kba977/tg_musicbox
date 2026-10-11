"""网易云音乐客户端，只依赖 httpx，不涉及 Telegram"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BASE_URL = 'https://music.163.com'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Referer': 'https://music.163.com/',
}
VIP_FEES = (1, 4)  # 1: VIP 歌曲, 4: 付费专辑，基本都无法下载

ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True)
class Music:
    id: int
    name: str
    author: str
    album: str
    thumb: str | None
    fee: int

    @property
    def outer_url(self) -> str:
        return f'{BASE_URL}/song/media/outer/url?id={self.id}.mp3'

    @property
    def title(self) -> str:
        return f'「{self.name}」- {self.author}'

    @property
    def vip(self) -> bool:
        return self.fee in VIP_FEES


class DownloadError(Exception):
    pass


def _parse_song(song: dict, artists_key: str, album_key: str) -> Music:
    album = song.get(album_key) or {}
    return Music(id=song['id'],
                 name=song['name'],
                 author='/'.join(a['name'] for a in song.get(artists_key) or []),
                 album=album.get('name') or '',
                 thumb=album.get('picUrl'),
                 fee=song.get('fee', 0))


def clean_lyric(raw: str) -> str:
    lines = re.sub(r'\[.*?\]', '', raw).split('\n')
    return '\n'.join(line.strip() for line in lines if line.strip())


class NeteaseClient:
    def __init__(self, proxy: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(headers=HEADERS, timeout=30, proxy=proxy, transport=transport,
                                       limits=httpx.Limits(max_connections=50))

    async def aclose(self):
        await self._http.aclose()

    async def search(self, keywords: str, limit: int = 8) -> list[Music]:
        resp = await self._http.get(f'{BASE_URL}/api/cloudsearch/pc',
                                    params={'s': keywords, 'type': 1, 'limit': limit})
        resp.raise_for_status()
        songs = (resp.json().get('result') or {}).get('songs') or []
        return [_parse_song(song, 'ar', 'al') for song in songs]

    async def get_song(self, music_id: int) -> Music | None:
        resp = await self._http.get(f'{BASE_URL}/api/song/detail/', params={'ids': f'[{music_id}]'})
        resp.raise_for_status()
        songs = resp.json().get('songs')
        return _parse_song(songs[0], 'artists', 'album') if songs else None

    async def get_lyric(self, music_id: int) -> str:
        try:
            resp = await self._http.get(f'{BASE_URL}/api/song/lyric', params={'id': music_id, 'lv': 1})
            return clean_lyric(resp.json()['lrc']['lyric'])
        except (httpx.HTTPError, KeyError, ValueError):
            logger.info('cannot find music lyric: %s', music_id)
            return ''

    async def resolve_download_url(self, music: Music) -> str | None:
        """outer url 会 302 到真实的 mp3 地址；VIP / 无版权 / 地区限制的歌曲则跳到 /404"""
        resp = await self._http.head(music.outer_url)
        location = resp.headers.get('location', '')
        if resp.status_code not in (301, 302) or not location or location.rstrip('/').endswith('/404'):
            return None
        return location

    async def download(self, url: str, max_size: int, on_progress: ProgressCallback | None = None) -> bytes:
        chunks = []
        received = 0
        async with self._http.stream('GET', url, follow_redirects=True) as resp:
            if resp.status_code != 200 or not resp.headers.get('content-type', '').startswith('audio'):
                raise DownloadError('该歌曲由于版权或者其他原因无法下载')
            total = int(resp.headers.get('content-length') or 0)
            if total > max_size:
                raise DownloadError(f'文件超过 {max_size // 1024 // 1024}MB，无法发送')

            async for data in resp.aiter_bytes(chunk_size=256 * 1024):
                chunks.append(data)
                received += len(data)
                if received > max_size:
                    raise DownloadError(f'文件超过 {max_size // 1024 // 1024}MB，无法发送')
                if on_progress and total:
                    await on_progress(received, total)
        return b''.join(chunks)

    async def get_thumb(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            resp = await self._http.get(url, params={'param': '320y320'}, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError:
            logger.info('cannot download thumb: %s', url)
            return None

    async def check_connectivity(self) -> bool:
        """启动自检：能否搜索、能否拿到免费歌曲的下载地址，用于发现地区限制"""
        try:
            musics = await self.search('周杰伦', limit=15)
        except (httpx.HTTPError, ValueError):
            logger.exception('网易云搜索接口无法访问，请检查网络或配置 NETEASE_PROXY')
            return False
        if not musics:
            logger.warning('网易云搜索结果为空，当前 IP 可能被限制，可配置 NETEASE_PROXY')
            return False

        candidates = [m for m in musics if not m.vip][:5]
        for music in candidates:
            if await self.resolve_download_url(music):
                logger.info('网易云连通性检查通过')
                return True
        logger.warning('网易云能搜索但免费歌曲也无法下载，当前 IP 可能被限制地区，可配置 NETEASE_PROXY')
        return False
