# -*- coding: utf-8 -*-

import os
import io
import re
from functools import wraps
import logging
from collections import namedtuple
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                     level=logging.INFO)

import requests
import telegram
from telegram import ChatAction
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from telegram.error import (TelegramError, Unauthorized, BadRequest, 
                            TimedOut, ChatMigrated, NetworkError)

# 机器人的 TOKEN 填写在这里
TOKEN = "{YOUR_BOT_TOKEN}" 
PORT = int(os.environ.get('PORT', '80'))

def error_callback(bot, context, error):
    try:
        raise error
    except Unauthorized:
        # remove update.message.chat_id from conversation list
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(1)')
    except BadRequest:
        # handle malformed requests - read more below!
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(2)')
    except TimedOut:
        # handle slow connection problems
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(3)')
    except NetworkError:
        # handle other connection problems
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(4)')
    except ChatMigrated as e:
        # the chat_id of a group has changed, use e.new_chat_id instead
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(5)')
    except TelegramError:
        # handle all other telegram related errors
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '网络不稳定, 请稍后再试(6)')


def getMusicNameFromUser(context):
    message = context.message.text
    if not message.startswith('我想听'):
        return None
    name = message.replace('我想听','').strip()
    return name


def getMusicInfoFromInternet(bot, context, musicName, msg):

    # 这里添加网易云音乐 API 的地址
    baseUrl = '{网易云音乐API的地址}'

    music = namedtuple('music_info', ['name', 'id', 'author', 'thumb', 'url', 'lyric'])
    resp = requests.get(baseUrl + f'/search?keywords={musicName}').json()
    music_info = None
    try:
        music_info = resp['result']['songs'][0]
    except KeyError as e:
        bot.edit_message_text(chat_id=context.message.chat_id,
                                message_id=msg.message_id, 
                                text='「'+ musicName +'」- 找不到该歌曲')
        return None
    
    music_id = music_info['id']
    music_name = music_info['name']
    music_author = music_info['artists'][0]['name']
    music_url = f'https://music.163.com/song/media/outer/url?id={music_id}.mp3'
    thumb_url = None
    music_lyric = None

    try:
        resp = requests.get(baseUrl + f'/song/detail?ids={music_id}').json()
        music_info = resp['songs'][0]
        thumb_url = music_info['al']['picUrl']
    except KeyError as e:
        logging.info('cannot find music detail info !')
    

    try:
        resp = requests.get(baseUrl + f'/lyric?id={music_id}').json()
        music_lyric = handle_lyric(resp['lrc']['lyric'])
    except KeyError as e:
        logging.info('cannot find music lyric !')

    return music(music_name, music_id, music_author, thumb_url, music_url, music_lyric)


def handle_lyric(old_lyric):
    lyric = re.sub(r'\[.*\]','', old_lyric).split('\n')
    return '\n'.join(l.strip() for l in lyric)


def downloadMusicWithProgress(bot, context, music, msg):
    logging.info(f'music: {music}')
    if music.url == '':
        return msg, None
    resp = requests.get(music.url, stream=True)
    total_length = resp.headers.get('Content-Length')
    dl = 0
    total_length = int(total_length)
    output = io.BytesIO()
    for data in resp.iter_content(chunk_size=1024*1024):
        output.write(data)
        dl += len(data)
        done = int(25 * dl / total_length)
        msg = bot.edit_message_text(chat_id=context.message.chat_id,
                      message_id=msg.message_id, 
                      text="「"+ music.name +"」- " + music.author + ", 下载中....\n\r`[%s%s]`" % ('█' * done, '.' * (25-done)),
                      parse_mode='Markdown')
    return msg, output


def echo(bot, context):
    name = getMusicNameFromUser(context)
    if not name: return

    msg = context.message.reply_text("「"+ name +"」 搜索中...", 
                                    reply_to_message_id=context.message.message_id)

    music = getMusicInfoFromInternet(bot, context, name, msg)
    if not music: return

    msg, output = downloadMusicWithProgress(bot, context, music, msg)

    if output is not None: 
        msg = bot.edit_message_text(chat_id=context.message.chat_id,
                                message_id=msg.message_id, 
                                text='mp3发送中....')

        bot.sendAudio(chat_id=context.message.chat.id, 
            audio=io.BytesIO(output.getvalue()), 
            timeout=3000, 
            title=music.name,
            performer=music.author,
            thumb=music.thumb,
            caption=music.lyric)
    else:
        bot.sendMessage(chat_id=context.message.chat.id, 
            text = '_该歌曲由于版权或者其他原因无法下载_\n\n' + music.author + ' - ' + music.name + '\n' + music.lyric,
            parse_mode='Markdown')

    bot.delete_message(chat_id=context.message.chat_id,
                       message_id=msg.message_id)


def main():
    updater = Updater(TOKEN)

    updater.start_webhook(listen="0.0.0.0",port=PORT,url_path=TOKEN)
    updater.bot.set_webhook("{部署代码的URL}" + TOKEN)

    dp = updater.dispatcher
    dp.add_handler(MessageHandler(Filters.text, echo))
    dp.add_error_handler(error_callback)

    updater.idle()


if __name__ == '__main__':
    main()