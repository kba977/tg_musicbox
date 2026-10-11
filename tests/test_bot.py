import asyncio
from collections import Counter
from types import SimpleNamespace as NS

import pytest
from telegram.constants import ChatType

from musicbox.bot import MusicBot, build_choices, extract_keywords
from musicbox.cache import FileIdCache
from musicbox.netease import DownloadError, Music

BOT = 'kba977_musicbox_bot'
FREE = Music(1, '晴天', '甲', '专辑', None, 8)
VIP = Music(2, '布拉格广场', '乙', '', None, 1)


class FakeNetease:
    def __init__(self, songs: list[Music]):
        self.songs = songs
        self.downloads = Counter()

    async def search(self, keywords, limit=8):
        await asyncio.sleep(0.01)
        return [] if keywords == '不存在' else self.songs[:limit]

    async def get_song(self, music_id):
        return next((m for m in self.songs if m.id == music_id), None)

    async def get_lyric(self, music_id):
        return '歌词'

    async def resolve_download_url(self, music):
        return None if music.vip else f'http://cdn/{music.id}.mp3'

    async def download(self, url, max_size, on_progress=None):
        self.downloads[url] += 1
        await asyncio.sleep(0.05)
        if on_progress:
            await on_progress(1, 1)
        return b'ID3...'

    async def get_thumb(self, url):
        return None


class FakeMessage:
    """记录所有 Telegram 调用的假消息对象"""

    def __init__(self, log: list, chat=ChatType.GROUP, text='', reply_to=None):
        self.log = log
        self.chat = NS(type=chat)
        self.text = text
        self.reply_to_message = reply_to
        self.audio_upload_delay = 0.05

    async def reply_text(self, text, **kw):
        self.log.append(('reply_text', text))
        return FakeMessage(self.log, self.chat.type, reply_to=self)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.log.append(('edit_text', text, reply_markup))

    async def delete(self):
        self.log.append(('delete',))

    async def reply_audio(self, audio, caption='', **kw):
        if isinstance(audio, str):
            self.log.append(('audio_file_id', audio))
        else:
            self.log.append(('audio_upload', kw['title']))
            await asyncio.sleep(self.audio_upload_delay)
        return NS(audio=NS(file_id=f'fid-{kw.get("title", audio)}'), caption=caption)


class FakeQuery:
    def __init__(self, data, user_id, message):
        self.data = data
        self.from_user = NS(id=user_id)
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


@pytest.fixture
def netease():
    return FakeNetease([FREE, VIP])


@pytest.fixture
def bot(netease):
    return MusicBot(netease, FileIdCache(':memory:'), max_downloads=2)


def update_for(message, user_id=42):
    return NS(effective_message=message, effective_user=NS(id=user_id), effective_chat=message.chat)


def kinds(log):
    return [entry[0] for entry in log]


@pytest.mark.parametrize('text, chat, expected', [
    ('晴天', ChatType.PRIVATE, None),
    ('我想听晴天', ChatType.PRIVATE, '晴天'),
    ('我想听', ChatType.PRIVATE, ''),
    ('我想听 晴天 ', ChatType.SUPERGROUP, '晴天'),
    (f'@{BOT.upper()} 晴天', ChatType.GROUP, '晴天'),
    (f'@{BOT} 我想听晴天', ChatType.GROUP, '晴天'),
    (f'@{BOT} 晴天', ChatType.PRIVATE, None),
    ('大家好', ChatType.GROUP, None),
])
def test_extract_keywords(text, chat, expected):
    assert extract_keywords(NS(text=text, chat=NS(type=chat)), BOT) == expected


def test_build_choices():
    text, keyboard = build_choices([FREE, VIP], user_id=42)
    assert '1. <b>晴天</b> - 甲 《专辑》' in text
    assert '2. <b>布拉格广场</b> - 乙 🔒' in text
    rows = keyboard.inline_keyboard
    assert [r[0].callback_data for r in rows] == ['pick:1:42', 'pick:2:42', 'cancel:0:42']


async def test_search_shows_choices(bot):
    log = []
    await bot.handle_request(update_for(FakeMessage(log)), '晴天')
    assert kinds(log) == ['reply_text', 'edit_text']
    assert log[-1][2] is not None   # 带选歌按钮


async def test_search_not_found(bot):
    log = []
    await bot.handle_request(update_for(FakeMessage(log)), '不存在')
    assert '找不到该歌曲' in log[-1][1]


async def test_single_result_downloads_directly(netease, bot):
    netease.songs = [FREE]
    log = []
    await bot.handle_request(update_for(FakeMessage(log)), '晴天')
    assert kinds(log) == ['reply_text', 'edit_text', 'audio_upload', 'delete']


async def test_empty_keywords_shows_help(bot):
    log = []
    await bot.handle_request(update_for(FakeMessage(log)), '')
    assert '我想听' in log[0][1]


async def test_choice_only_by_requester(bot):
    log = []
    user_msg = FakeMessage(log)
    status = FakeMessage(log, reply_to=user_msg)

    query = FakeQuery('pick:1:42', 99, status)
    await bot.on_choice(NS(callback_query=query), None)
    assert '不是你点的歌' in query.answers[0]
    assert log == []

    query = FakeQuery('pick:1:42', 42, status)
    await bot.on_choice(NS(callback_query=query), None)
    assert kinds(log) == ['edit_text', 'edit_text', 'audio_upload', 'delete']


async def test_choice_vip_returns_lyric(bot):
    log = []
    status = FakeMessage(log, reply_to=FakeMessage(log))
    await bot.on_choice(NS(callback_query=FakeQuery('pick:2:42', 42, status)), None)
    assert '无法下载' in log[-1][1] and '歌词' in log[-1][1]
    assert 'delete' not in kinds(log)


async def test_choice_cancel(bot):
    log = []
    await bot.on_choice(NS(callback_query=FakeQuery('cancel:0:42', 42, FakeMessage(log))), None)
    assert kinds(log) == ['delete']


async def test_download_error_shown_in_status(netease, bot):
    async def broken(*a, **kw):
        raise DownloadError('文件超过 50MB，无法发送')
    netease.download = broken
    log = []
    status = FakeMessage(log, reply_to=FakeMessage(log))
    await bot.download_music(status, FREE)
    assert '文件超过 50MB' in log[-1][1]


async def test_user_cannot_flood(bot):
    log = []
    msg = FakeMessage(log)
    await asyncio.gather(bot.handle_request(update_for(msg), '晴天'),
                         bot.handle_request(update_for(msg), '晴天'))
    assert ('reply_text', '上一首还在处理中，请稍等～') in log
    assert len(bot.user_slot) == 0


async def test_concurrent_same_song_downloads_once(netease, bot):
    log = []
    statuses = [FakeMessage(log, reply_to=FakeMessage(log)) for _ in range(10)]
    await asyncio.gather(*(bot.download_music(s, FREE) for s in statuses))

    assert sum(netease.downloads.values()) == 1
    counts = Counter(kinds(log))
    assert counts['audio_upload'] == 1 and counts['audio_file_id'] == 9 and counts['delete'] == 10
    assert len(bot.song_lock) == 0


async def test_progress_only_in_private_chat(bot):
    log = []
    private = FakeMessage(log, chat=ChatType.PRIVATE)
    status = FakeMessage(log, chat=ChatType.PRIVATE, reply_to=private)
    await bot.download_music(status, Music(3, 'a', 'b', '', None, 0))
    assert any('下载中' in e[1] for e in log if e[0] == 'edit_text')

    log.clear()
    group = FakeMessage(log)
    await bot.download_music(FakeMessage(log, reply_to=group), Music(4, 'a', 'b', '', None, 0))
    assert not any('下载中' in e[1] for e in log if e[0] == 'edit_text')
