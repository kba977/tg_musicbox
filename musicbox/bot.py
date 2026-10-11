"""Telegram 交互：解析点歌消息、选歌按钮、下载并发送歌曲"""

import asyncio
import html
import logging
import re
import time

from telegram import (
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .access import AccessControl, AllowStore, ChangeResult, Kind
from .cache import BusySet, FileIdCache, KeyedLock
from .config import Settings
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
DENIED_PRIVATE = '🔒 这是一个私人 bot，暂不对外开放\n\n你的用户 ID：<code>{id}</code>，可以发给管理员申请开通'
DENIED_GROUP = '🔒 本群未开通，群 ID：<code>{id}</code>'
ADMIN_ONLY = '⛔ 仅管理员可以使用该命令'
ADMIN_HELP = (
    '<b>白名单管理</b>\n'
    '/allow <code>ID</code> - 加入白名单（正数为用户，负数为群）\n'
    '/deny <code>ID</code> - 移出白名单\n'
    '/allowlist - 查看白名单\n\n'
    '也可以回复某人的消息发送 /allow，或在群里直接发送 /allow 开通本群'
)
USER_COMMANDS = [('music', '搜索并下载歌曲，例如 /music 晴天'), ('help', '使用说明')]
ADMIN_COMMANDS = USER_COMMANDS + [('allow', '加入白名单'), ('deny', '移出白名单'),
                                  ('allowlist', '查看白名单')]


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


def format_ids(ids: list[int]) -> str:
    return '、'.join(f'<code>{i}</code>' for i in ids) or '无'


def progress_text(music: Music, received: int, total: int) -> str:
    done = PROGRESS_WIDTH * received // total
    bar = '█' * done + '.' * (PROGRESS_WIDTH - done)
    return f'{html.escape(music.title)}, 下载中....\n<code>[{bar}]</code>'


class MusicBot:
    def __init__(self, netease: NeteaseClient, cache: FileIdCache, access: AccessControl,
                 max_downloads: int = 5):
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

    # ---------- 白名单管理（仅管理员） ----------

    async def _check_admin(self, update: Update) -> bool:
        if self.access.is_admin(update.effective_user.id):
            return True
        await update.effective_message.reply_text(ADMIN_ONLY)
        return False

    async def _resolve_target(self, update: Update, args: list[str]) -> tuple[int, str] | None:
        """命令的目标：参数里的 ID > 被回复的人 > 当前群；返回 (ID, 描述)"""
        message, chat = update.effective_message, update.effective_chat
        if args:
            try:
                target = int(args[0])
            except ValueError:
                await message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)
                return None
            kind = '用户' if Kind.of(target) == Kind.USER else '群'
            return target, f'{kind} <code>{target}</code>'

        replied = message.reply_to_message
        if replied and replied.from_user and not replied.from_user.is_bot:
            user = replied.from_user
            return user.id, f'用户 {html.escape(user.full_name)}（<code>{user.id}</code>）'
        if chat.type != ChatType.PRIVATE:
            return chat.id, f'本群 {html.escape(chat.title or "")}（<code>{chat.id}</code>）'

        await message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)
        return None

    async def on_allow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        target = await self._resolve_target(update, context.args)
        if not target:
            return
        target_id, label = target
        if self.access.add(target_id, update.effective_user.id) == ChangeResult.DONE:
            logger.info('allowlist add %s by %s', target_id, update.effective_user.id)
            text = f'✅ 已将{label}加入白名单'
        else:
            text = f'{label} 已经在白名单中'
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def on_deny(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        target = await self._resolve_target(update, context.args)
        if not target:
            return
        target_id, label = target
        if self.access.is_admin(target_id):
            await update.effective_message.reply_text(f'{label} 是管理员，始终可以使用',
                                                      parse_mode=ParseMode.HTML)
            return
        result = self.access.remove(target_id)
        if result == ChangeResult.DONE:
            logger.info('allowlist remove %s by %s', target_id, update.effective_user.id)
        text = {ChangeResult.DONE: f'🚫 已将{label}移出白名单',
                ChangeResult.UNCHANGED: f'{label} 不在白名单中',
                ChangeResult.STATIC: f'{label} 是在 .env 中配置的，需要修改 .env 后重启'}[result]
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def on_allowlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        config = self.access.config
        lines = [f'<b>访问模式</b>：{"公开，所有人可用" if config.public else "私有，仅白名单可用"}',
                 f'<b>管理员</b>：{format_ids(sorted(config.admins))}']
        for kind, (static, dynamic) in self.access.listing().items():
            name = '用户' if kind == Kind.USER else '群'
            lines.append(f'<b>{name}</b>：{format_ids(dynamic)}')
            if static:
                lines.append(f'<b>{name}（.env）</b>：{format_ids(static)}')
        await update.effective_message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)

    @staticmethod
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error('Exception while handling an update:', exc_info=context.error)


def build_application(settings: Settings) -> Application:
    netease = NeteaseClient(proxy=settings.netease_proxy)
    cache = FileIdCache(settings.cache_db)
    allow_store = AllowStore(settings.cache_db)
    bot = MusicBot(netease, cache, AccessControl(settings.access, allow_store), settings.max_downloads)

    async def post_init(app: Application):
        await app.bot.set_my_commands(USER_COMMANDS)
        # 管理员命令只在管理员的私聊命令菜单里显示
        for admin in settings.access.admins:
            try:
                await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(admin))
            except TelegramError:
                logger.warning('无法为管理员 %s 设置命令菜单（需要先私聊 bot 一次）', admin)

        listing = bot.access.listing()
        users = sum(len(ids) for ids in listing[Kind.USER])
        chats = sum(len(ids) for ids in listing[Kind.CHAT])
        if settings.access.public:
            logger.info('访问模式：公开，所有人可用')
        else:
            logger.info('访问模式：私有，管理员 %d 个、白名单用户 %d 个、群 %d 个',
                        len(settings.access.admins), users, chats)
        if not settings.access.admins:
            logger.warning('未配置 ADMIN_USERS，无法用命令管理白名单。私聊 bot 获取你的用户 ID 后填入')
        await netease.check_connectivity()

    async def post_shutdown(app: Application):
        await netease.aclose()
        cache.close()
        allow_store.close()

    app = (Application.builder()
           .token(settings.token)
           .concurrent_updates(True)
           .rate_limiter(AIORateLimiter(max_retries=3))
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    app.add_handler(CommandHandler(['start', 'help'], bot.on_help))
    app.add_handler(CommandHandler('music', bot.on_music_command))
    app.add_handler(CommandHandler('allow', bot.on_allow))
    app.add_handler(CommandHandler('deny', bot.on_deny))
    app.add_handler(CommandHandler('allowlist', bot.on_allowlist))
    app.add_handler(CallbackQueryHandler(bot.on_choice, pattern=r'^(pick|cancel):'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.add_error_handler(bot.on_error)
    return app
