import time
import asyncio
import httpx
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import register, Star, Context

@register("astrbot_plugin_ncm_directlink", "monbed", "获取网易云音乐直链插件", "1.2.4", "https://github.com/monbed/astrbot_plugin_ncm_directlink")
class DownloadMusicPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cookie = config.get('cookie', '')
        self.apiurl = config.get('apiurl', '').rstrip('/')
        self.level = config.get('level', '')
        self.limit = config.get('limit', '')
        self.timeout = config.get('timeout', 10.0)
        self._lock = asyncio.Lock()
        self._last_req = 0.0
        self._search_cache = {}
        self._client = httpx.AsyncClient(timeout=self.timeout)

    @staticmethod
    def _format_song(song: dict) -> str:
        """格式化歌曲信息为 '歌名 - 歌手 [专辑]' 格式"""
        name = song.get("name", "未知")
        artist = ", ".join(a.get("name", "") for a in song.get("ar", []))
        album = song.get("al", {}).get("name", "")
        return f"{name} - {artist} [{album}]"

    @staticmethod
    def _get_session_id(event: AstrMessageEvent):
        return getattr(event, 'session_id', None) or getattr(event, 'unified_msg_origin', None)

    async def _send(self, event: AstrMessageEvent, content: str):
        await self.context.send_message(
            session=event.session,
            message_chain=event.chain_result([Plain(content)])
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
        try:
            songs = await self._get_musicids(music_name)
            if not songs:
                await self._send(event, f"❌ 未找到歌曲「{music_name}」")
                return

            # 发送歌曲列表
            msg = "搜索结果：\n"
            for i, song in enumerate(songs, 1):
                msg += f"{i}. {self._format_song(song)}\n"
            msg += "请回复序号获取直链（60秒内有效）"
            await self._send(event, msg)

            # 保存搜索结果到上下文，等待用户输入序号
            session_id = self._get_session_id(event)
            self._search_cache[session_id] = {
                'songs': songs,
                'timestamp': time.time(),
                'user_id': event.get_sender_id(),
            }
        except Exception as e:
            logger.error(f"处理异常: {e}", exc_info=True)
            await self._send(event, "❌ 搜索或缓存歌曲时发生异常")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_music_index(self, event: AstrMessageEvent):
        now = time.time()
        # 清理非常过期的缓存（例如5分钟以上的），避免刚满60秒的用户的缓存被其他人的消息误删
        expired = [k for k, v in self._search_cache.items() if now - v['timestamp'] > 300]
        for k in expired:
            del self._search_cache[k]

        session_id = self._get_session_id(event)
        cache = self._search_cache.get(session_id)
        if not cache:
            return

        # 只允许触发指令的用户回复序号
        if event.get_sender_id() != cache['user_id']:
            return

        text = event.message_str.strip()
        
        # 检查当前用户的会话是否过期，给予提示
        if now - cache['timestamp'] > 60:
            del self._search_cache[session_id]
            if text.isdigit():
                await self._send(event, "❌ 回复超时，请重新发送指令")
            return

        if not text.isdigit():
            return

        songs = cache['songs']
        idx = int(text)
        if not (1 <= idx <= len(songs)):
            return

        song = songs[idx - 1]
        info = self._format_song(song)
        url = await self._get_music_url(song.get("id"))
        msg = f"✅ {info}\n直链：{url}" if url else f"❌ {info}\n获取直链失败"
        await self._send(event, msg)
        # 用完即删
        del self._search_cache[session_id]

    async def _get_musicids(self, keyword: str) -> list[dict]:
        search_url = f"{self.apiurl}/cloudsearch"
        params = {"keywords": keyword, "type": 1}
        if self.limit:
            params['limit'] = self.limit
        result = await self.api_request(search_url, params)
        songs = (result.get('result') or {}).get('songs') if isinstance(result, dict) else None
        return songs or []

    async def _get_music_url(self, song_id: int) -> str | None:
        enhanced_url = f"{self.apiurl}/song/download/url/v1"
        params = {"id": song_id}
        if self.level:
            params['level'] = self.level
        if self.cookie:
            params["cookie"] = self.cookie
        try:
            result = await self.api_request(enhanced_url, params)
            return (result.get('data') or {}).get('url') if isinstance(result, dict) else None
        except Exception as e:
            logger.error(f"_get_music_url 异常: {e}")
        return None
