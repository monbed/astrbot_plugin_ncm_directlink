import time
import asyncio
import httpx
import os
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
try:
    from astrbot.api.message_components import MessageChain
except ImportError:
    class MessageChain(list):
        def __init__(self, chain=None):
            super().__init__(chain or [])
            self.chain = self

from astrbot.api.star import register, Star, Context

@register("astrbot_plugin_ncm_directlink", "monbed", "获取网易云音乐直链插件", "1.2.1", "https://github.com/monbed/astrbot_plugin_ncm_directlink")
class DownloadMusicPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cookie = config.get('cookie', '')
        self.apiurl = config.get('apiurl', '')
        self.level = config.get('level', '')
        self.limit = config.get('limit', '')
        self._lock = asyncio.Lock()
        self._last_req = 0.0
        self._client = httpx.AsyncClient(
            verify=False,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko"},
            timeout=10.0
        )

    async def api_request(self, url: str, params: dict) -> dict:
        async with self._lock:
            wait = 1.0 - (time.time() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            self._last_req = time.time()
            return resp.json()

    @filter.command("下载音乐")
    async def download_music(self, event: AstrMessageEvent, music_name: str):
        music_name = event.message_str.strip()
        if music_name.startswith("下载音乐"):
            music_name = music_name[len("下载音乐"):].strip()
        def build_chain(content):
            return event.chain_result([Plain(content)])

        try:
            songs = await self._get_musicids(music_name)
            if not songs:
                await self.context.send_message(
                    session=event.session,
                    message_chain=build_chain(f"❌ 未找到歌曲「{music_name}」")
                )
                return

            # 发送歌曲列表
            msg = "搜索结果：\n"
            for i, song in enumerate(songs, 1):
                name = song.get("name", "未知")
                artist = ", ".join([a.get("name", "") for a in song.get("artists", [])])
                album = song.get("album", {}).get("name", "")
                msg += f"{i}. {name} - {artist} [{album}]\n"
            msg += "请回复序号获取直链（30秒内有效）"
            await self.context.send_message(
                session=event.session,
                message_chain=build_chain(msg)
            )

            # 保存搜索结果到上下文，等待用户输入序号，包含时间戳和用户ID（官方推荐写法）
            if not hasattr(self, '_search_cache'):
                self._search_cache = {}
            session_id = getattr(event, 'session_id', None) or getattr(event, 'unified_msg_origin', None)

            self._search_cache[session_id] = {
                'songs': songs,
                'timestamp': time.time(),
                'user_id': event.get_sender_id(),
                'session': event.session  # 保存session用于后续发消息
            }

            # 启动超时提醒任务
            asyncio.create_task(self._timeout_reminder(session_id))
        except Exception as e:
            import traceback
            logger.error(f"处理异常: {str(e)}\n{traceback.format_exc()}")
            await self.context.send_message(
                session=event.session,
                message_chain=build_chain("❌ 搜索或缓存歌曲时发生异常")
            )
    async def _timeout_reminder(self, session_id):
        try:
            await asyncio.sleep(30)
            cache = self._search_cache.get(session_id)
            if cache:
                # 30秒后还在，说明用户未回复，自动提醒
                session = cache.get('session')
                await self.context.send_message(
                    session=session,
                    message_chain=MessageChain([Plain("❌ 回复超时，请重新发送指令")])
                )
                del self._search_cache[session_id]
        except Exception as e:
            logger.error(f"处理异常: {str(e)}")
            # session_id 可能已被删除，无法获取 session，故只记录日志

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_music_index(self, event: AstrMessageEvent):
        def build_chain(content):
            return event.chain_result([Plain(content)])
        # 检查是否有缓存的搜索结果
        if not hasattr(self, '_search_cache'):
            return
        session_id = getattr(event, 'session_id', None) or getattr(event, 'unified_msg_origin', None)
        cache = self._search_cache.get(session_id)
        if not cache:
            return
        songs = cache.get('songs')
        timestamp = cache.get('timestamp', 0)
        user_id = cache.get('user_id')
        # 获取当前事件的用户ID（官方推荐写法）
        current_user_id = event.get_sender_id()
        # 只允许触发指令的用户回复序号
        if current_user_id != user_id:
            return
        # 超时处理，30秒
        if time.time() - timestamp > 30:
            await self.context.send_message(
                session=event.session,
                message_chain=build_chain("❌ 回复超时，请重新发送指令")
            )
            del self._search_cache[session_id]
            return
        text = event.message_str.strip()
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(songs):
                song = songs[idx-1]
                url = await self._get_music_url(song.get("id"))
                name = song.get("name", "未知")
                artist = ", ".join([a.get("name", "") for a in song.get("artists", [])])
                album = song.get("album", {}).get("name", "")
                if url:
                    msg = f"✅ {name} - {artist} [{album}]\n直链：{url}"
                else:
                    msg = f"❌ {name} - {artist} [{album}]\n获取直链失败"
                await self.context.send_message(
                    session=event.session,
                    message_chain=build_chain(msg)
                )
                # 用完即删
                del self._search_cache[session_id]

    async def _get_musicids(self, keyword: str) -> list[dict]:
        search_url = f"{self.apiurl.rstrip('/')}/search"
        params = {"keywords": keyword}
        if self.limit:
            params['limit'] = self.limit
        params['type'] = 1
        result = await self.api_request(search_url, params)
        songs = None
        if isinstance(result, dict):
            songs = (result.get('result') or {}).get('songs') if result.get('result') else None
            if not songs:
                songs = result.get('songs')
            if not songs:
                songs = (result.get('data') or {}).get('songs')
        return songs or []

    async def _get_music_url(self, song_id: str) -> str | None:
        enhanced_url = f"{self.apiurl.rstrip('/')}/song/download/url/v1"
        params = {"id": song_id}
        if self.level:
            params['level'] = self.level
        if self.cookie:
            params["cookie"] = self.cookie
        try:
            result = await self.api_request(enhanced_url, params)
            if isinstance(result, dict):
                data = result.get('data')
                if isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    url = first.get('url') or first.get('data') or first.get('mp3')
                    if url:
                        return url
                elif isinstance(data, dict):
                    return data.get('url')
        except Exception as e:
            logger.error(f"_get_music_url 异常: {e}")
        return None

    async def __del__(self):
        await self._client.aclose()
