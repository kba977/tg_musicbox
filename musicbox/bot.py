"""Telegram 交互：解析点歌消息、选歌按钮、下载并发送歌曲"""

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .cache import BusySet, FileIdCache, KeyedLock
from .config import Access, Settings
from .netease import DownloadError, Music, NeteaseClient

logger = logging.getLogger(__name__)

TRIGGER = '我想听'
SEARCH_LIMIT = 8
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MAX_AUDIO_SIZE = 50 * 1024 * 1024   # bot 上传文件上限
PROGRESS_WIDTH = 25
PROGRESS_INTERVAL = 1.5             # 进度条最短刷新间隔(秒)，避免触发 Telegram 限流

HELP_TEXT = (
    '🎵 我可以帮你从网易云音乐找歌\n\n'
    '发送 <code>我想听歌名</code> 或 <code>/music 歌名</code>，例如 <code>我想听晴天</code>\n'
    '搜到多首时会列出来让你选择\n\n'
    '<i>注：🔒 标记的是 VIP / 付费歌曲，通常无法下载，会返回歌词</i>'
)
UNAVAILABLE = '该歌曲由于版权或者其他原因无法下载'
DENIED_PRIVATE = '🔒 这是一个私人 bot，暂不对外开放\n\n你的用户 ID：<code>{id}</code>'
DENIED_GROUP = '🔒 本群未开通，群 ID：<code>{id}</code>'


def extract_keywords(message: Message, bot_username: str) -> str | None:
    """只响应「我想听xxx」（群聊里也可以「@bot xxx」），无关消息返回 None"""
    text, mentioned = re.subn(f'@{re.escape(bot_username)}', '', message.text or '', flags=re.IGNORECASE)
    text = text.strip()
    if text.startswith(TRIGGER):
        return text[len(TRIGGER):].strip()
    if mentioned and message.chat.type != ChatType.PRIVATE:
        return text
    return None


def build_choices(musics: list[Music], user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = []
    buttons = []
    for i, m in enumerate(musics, 1):
        lock = ' 🔒' if m.vip else ''
        album = f' 《{html.escape(m.album)}》' if m.album else ''
        lines.append(f'{i}. <b>{html.escape(m.name)}</b> - {html.escape(m.author)}{album}{lock}')
        buttons.append([InlineKeyboardButton(f'{i}. {m.name} - {m.author}{lock}'[:60],
                                             callback_data=f'pick:{m.id}:{user_id}')])
    buttons.append([InlineKeyboardButton('❌ 取消', callback_data=f'cancel:0:{user_id}')])
    return '找到以下歌曲，请选择：\n\n' + '\n'.join(lines), InlineKeyboardMarkup(buttons)


def progress_text(music: Music, received: int, total: int) -> str:
    done = PROGRESS_WIDTH * received // total
    bar = '█' * done + '.' * (PROGRESS_WIDTH - done)
    return f'{html.escape(music.title)}, 下载中....\n<code>[{bar}]</code>'


class MusicBot:
    def __init__(self, netease: NeteaseClient, cache: FileIdCache, access: Access, max_downloads: int = 5):
        self.netease = netease
        self.cache = cache
        self.access = access
        self.download_slots = asyncio.Semaphore(max_downloads)
        self.song_lock = KeyedLock()    # 同一首歌并发请求时排队，第一个上传后其余走缓存
        self.user_slot = BusySet()      # 每个人同一时间只处理一个请求，防止刷屏

    # ---------- 下载与发送 ----------

    async def _send_cached(self, message: Message, music: Music) -> bool:
        cached = self.cache.get(music.id)
        if not cached:
            return False
        file_id, caption = cached
        try:
            await message.reply_audio(audio=file_id, caption=caption)
            return True
        except BadRequest:
            logger.warning('cached file_id invalid, re-downloading: %s', music.id)
            self.cache.delete(music.id)
            return False

    async def _deliver(self, status: Message, music: Music) -> bool:
        """发送歌曲，成功返回 True；失败时把原因写到 status 消息里并返回 False"""
        # 群聊里 status 是对点歌消息的回复，歌曲也回复到点歌消息上
        message = status.reply_to_message or status
        if await self._send_cached(message, music):
            return True

        lyric_task = asyncio.create_task(self.netease.get_lyric(music.id))
        thumb_task = asyncio.create_task(self.netease.get_thumb(music.thumb))
        try:
            async with self.download_slots:
                url = await self.netease.resolve_download_url(music)
                if not url:
                    raise DownloadError(UNAVAILABLE)
                on_progress = self._progress_updater(status, music) \
                    if status.chat.type == ChatType.PRIVATE else None
                audio = await self.netease.download(url, MAX_AUDIO_SIZE, on_progress)
        except DownloadError as e:
            text = f'<i>{html.escape(str(e))}</i>\n\n{html.escape(music.title)}'
            thumb_task.cancel()
            lyric = await lyric_task
            if lyric:
                text += '\n\n' + html.escape(lyric)
            await status.edit_text(text[:MESSAGE_LIMIT], parse_mode=ParseMode.HTML)
            return False

        await status.edit_text(f'{html.escape(music.title)}, 发送中....', parse_mode=ParseMode.HTML)
        thumb, lyric = await asyncio.gather(thumb_task, lyric_task)
        sent = await message.reply_audio(audio=InputFile(audio, filename=f'{music.name}.mp3'),
                                         title=music.name,
                                         performer=music.author,
                                         thumbnail=thumb,
                                         caption=lyric[:CAPTION_LIMIT],
                                         write_timeout=300)
        self.cache.set(music.id, sent.audio.file_id, sent.caption or '')
        return True

    @staticmethod
    def _progress_updater(status: Message, music: Music):
        last_edit = 0.0

        async def update(received: int, total: int):
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit >= PROGRESS_INTERVAL:
                last_edit = now
                await status.edit_text(progress_text(music, received, total), parse_mode=ParseMode.HTML)
        return update

    async def download_music(self, status: Message, music: Music):
        try:
            async with self.song_lock(music.id):
                delivered = await self._deliver(status, music)
            if delivered:
                await status.delete()
        except Exception:
            logger.exception('failed to deliver %s', music)
            await status.edit_text(f'{html.escape(music.title)} - 出错了，请稍后再试',
                                   parse_mode=ParseMode.HTML)

    async def search_and_choose(self, message: Message, user_id: int, keywords: str):
        status = await message.reply_text(f'「{keywords}」 搜索中...')
        try:
            musics = await self.netease.search(keywords, SEARCH_LIMIT)
        except Exception:
            logger.exception('failed to search %r', keywords)
            await status.edit_text(f'「{keywords}」- 搜索出错了，请稍后再试')
            return

        if not musics:
            await status.edit_text(f'「{keywords}」- 找不到该歌曲')
        elif len(musics) == 1:
            await self.download_music(status, musics[0])
        else:
            text, keyboard = build_choices(musics, user_id)
            await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # ---------- Telegram handlers ----------

    async def _check_access(self, update: Update) -> bool:
        """没有权限时回复提示（附上 ID，方便加入白名单）并返回 False"""
        user, chat = update.effective_user, update.effective_chat
        if self.access.allows(user.id if user else None, chat.id if chat else None):
            return True

        logger.info('access denied: user=%s (@%s) chat=%s (%s)',
                    user and user.id, user and user.username, chat and chat.id, chat and chat.type)
        if update.callback_query:
            await update.callback_query.answer('🔒 你没有使用权限', show_alert=True)
        elif chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text(DENIED_PRIVATE.format(id=user.id),
                                                      parse_mode=ParseMode.HTML)
        else:
            await update.effective_message.reply_text(DENIED_GROUP.format(id=chat.id),
                                                      parse_mode=ParseMode.HTML)
        return False

    async def handle_request(self, update: Update, keywords: str):
        message = update.effective_message
        if not keywords:
            await message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
            return

        async with self.user_slot(update.effective_user.id) as ok:
            if not ok:
                await message.reply_text('上一首还在处理中，请稍等～')
                return
            await self.search_and_choose(message, update.effective_user.id, keywords)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keywords = extract_keywords(update.effective_message, context.bot.username)
        if keywords is None and update.effective_chat.type != ChatType.PRIVATE:
            return   # 群里的闲聊，不理会
        if not await self._check_access(update):
            return
        if keywords is None:
            await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
        else:
            await self.handle_request(update, keywords)

    async def on_music_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._check_access(update):
            await self.handle_request(update, ' '.join(context.args).strip())

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._check_access(update):
            await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    async def on_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not await self._check_access(update):
            return
        action, music_id, owner_id = query.data.split(':')
        if query.from_user.id != int(owner_id):
            await query.answer('这不是你点的歌哦，自己发「我想听xxx」吧', show_alert=True)
            return

        status = query.message
        if action == 'cancel':
            await query.answer('已取消')
            await status.delete()
            return

        async with self.user_slot(query.from_user.id) as ok:
            if not ok:
                await query.answer('上一首还在处理中，请稍等～')
                return
            await query.answer()
            try:
                music = await self.netease.get_song(int(music_id))
            except Exception:
                logger.exception('failed to get song %s', music_id)
                await status.edit_text('获取歌曲信息出错了，请稍后再试')
                return
            if not music:
                await status.edit_text('找不到该歌曲')
                return
            await status.edit_text(f'{html.escape(music.title)}, 下载中....', parse_mode=ParseMode.HTML)
            await self.download_music(status, music)

    @staticmethod
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error('Exception while handling an update:', exc_info=context.error)


def build_application(settings: Settings) -> Application:
    netease = NeteaseClient(proxy=settings.netease_proxy)
    cache = FileIdCache(settings.cache_db)
    bot = MusicBot(netease, cache, settings.access, settings.max_downloads)

    async def post_init(app: Application):
        await app.bot.set_my_commands([('music', '搜索并下载歌曲，例如 /music 晴天'),
                                       ('help', '使用说明')])
        access = settings.access
        if access.public:
            logger.info('访问模式：公开，所有人可用')
        else:
            logger.info('访问模式：私有，白名单用户 %d 个、群 %d 个', len(access.users), len(access.chats))
            if not access.users and not access.chats:
                logger.warning('白名单为空，没有人能使用 bot。私聊 bot 获取你的用户 ID，填入 ALLOWED_USERS')
        await netease.check_connectivity()

    async def post_shutdown(app: Application):
        await netease.aclose()
        cache.close()

    app = (Application.builder()
           .token(settings.token)
           .concurrent_updates(True)
           .rate_limiter(AIORateLimiter(max_retries=3))
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    app.add_handler(CommandHandler(['start', 'help'], bot.on_help))
    app.add_handler(CommandHandler('music', bot.on_music_command))
    app.add_handler(CallbackQueryHandler(bot.on_choice, pattern=r'^(pick|cancel):'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.add_error_handler(bot.on_error)
    return app
