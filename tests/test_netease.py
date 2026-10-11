import os

import httpx
import pytest

from musicbox.netease import DownloadError, Music, NeteaseClient, clean_lyric

SEARCH_RESPONSE = {'result': {'songs': [
    {'id': 1, 'name': '晴天', 'ar': [{'name': 'A'}, {'name': 'B'}],
     'al': {'name': '专辑', 'picUrl': 'http://p.music.126.net/1.jpg'}, 'fee': 8},
    {'id': 2, 'name': 'VIP歌', 'ar': [{'name': 'C'}], 'al': None, 'fee': 1},
]}}


def make_client(handler) -> NeteaseClient:
    return NeteaseClient(transport=httpx.MockTransport(handler))


def music(id=1, fee=0) -> Music:
    return Music(id=id, name='歌', author='人', album='', thumb=None, fee=fee)


def test_clean_lyric():
    raw = '[00:00.000] 作词 : 方文山\n[00:01.000]\n[00:12.34]故事的小黄花\n'
    assert clean_lyric(raw) == '作词 : 方文山\n故事的小黄花'


async def test_search_parses_songs():
    async def handler(request):
        assert request.url.path == '/api/cloudsearch/pc'
        assert request.url.params['s'] == '晴天'
        return httpx.Response(200, json=SEARCH_RESPONSE)

    client = make_client(handler)
    songs = await client.search('晴天')
    assert songs[0] == Music(1, '晴天', 'A/B', '专辑', 'http://p.music.126.net/1.jpg', 8)
    assert not songs[0].vip
    assert songs[1].album == '' and songs[1].thumb is None and songs[1].vip
    await client.aclose()


async def test_search_empty():
    client = make_client(lambda r: httpx.Response(200, json={'result': {}, 'code': 200}))
    assert await client.search('不存在') == []
    await client.aclose()


async def test_get_song():
    detail = {'songs': [{'id': 7, 'name': '屋顶', 'artists': [{'name': '周杰伦'}],
                         'album': {'name': 'X', 'picUrl': 'u'}, 'fee': 8}]}
    client = make_client(lambda r: httpx.Response(200, json=detail))
    assert await client.get_song(7) == Music(7, '屋顶', '周杰伦', 'X', 'u', 8)
    await client.aclose()


@pytest.mark.parametrize('status, location, expected', [
    (302, 'http://m801.music.126.net/a.mp3', 'http://m801.music.126.net/a.mp3'),
    (302, 'http://music.163.com/404', None),
    (200, '', None),
])
async def test_resolve_download_url(status, location, expected):
    headers = {'location': location} if location else {}
    client = make_client(lambda r: httpx.Response(status, headers=headers))
    assert await client.resolve_download_url(music()) == expected
    await client.aclose()


async def test_download_reports_progress():
    body = b'x' * (600 * 1024)
    client = make_client(lambda r: httpx.Response(200, content=body, headers={'content-type': 'audio/mpeg'}))
    progress = []

    async def on_progress(received, total):
        progress.append((received, total))

    assert await client.download('http://cdn/a.mp3', 10 * 1024 * 1024, on_progress) == body
    assert progress[-1] == (len(body), len(body))
    await client.aclose()


async def test_download_rejects_html():
    client = make_client(lambda r: httpx.Response(200, text='<html>404</html>',
                                                  headers={'content-type': 'text/html'}))
    with pytest.raises(DownloadError):
        await client.download('http://cdn/a.mp3', 1024)
    await client.aclose()


async def test_download_rejects_too_large():
    client = make_client(lambda r: httpx.Response(200, content=b'x' * 2048,
                                                  headers={'content-type': 'audio/mpeg'}))
    with pytest.raises(DownloadError, match='无法发送'):
        await client.download('http://cdn/a.mp3', 1024)
    await client.aclose()


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get('NETEASE_LIVE'), reason='设置 NETEASE_LIVE=1 才访问真实接口')
async def test_live_connectivity():
    client = NeteaseClient(proxy=os.environ.get('NETEASE_PROXY'))
    assert await client.check_connectivity()
    await client.aclose()
