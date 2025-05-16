import time
import asyncio
import httpx
import os
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain

@register("astrbot_plugin_ncm_directlink", "monbed", "获取网易云音乐直链插件", "1.0.2", "https://github.com/monbed/astrbot_plugin_ncm_directlink")
class DownloadMusicPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.token = config.get('token', '')
        self.cookie = config.get('cookie', '')
        self._lock = asyncio.Lock()
        self._last_req = 0.0
        self._client = httpx.AsyncClient(
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
        # 使用事件对象构造消息链
        def build_chain(content):
            return event.chain_result([Plain(content)])

        if not self.token:
            await self.context.send_message(
                session=event.session,
                message_chain=build_chain("❌ 未配置音乐API Token")
            )
            return

        try:
            song_id = await self._get_musicid(music_name)
            if not song_id:
                await self.context.send_message(
                    session=event.session,
                    message_chain=build_chain(f"❌ 未找到歌曲「{music_name}」")
                )
                return

            url = await self._get_music_url(song_id)
            if not url:
                await self.context.send_message(
                    session=event.session,
                    message_chain=build_chain("❌ 获取下载链接失败")
                )
                return

            await self.context.send_message(
                session=event.session,
                message_chain=event.chain_result([
                    Plain(f"✅ 歌曲「{music_name}」下载链接："),
                    Plain(url)
                ])
            )
        except Exception as e:
            logger.error(f"处理异常: {str(e)}")
            await self.context.send_message(
                session=event.session,
                message_chain=build_chain("❌ 服务暂时不可用")
            )

    async def _get_musicid(self, keyword: str) -> str | None:
        params = {"token": self.token, "keyword": keyword, "limit": 1, "type": 1}
        result = await self.api_request("https://v3.alapi.cn/api/music/search", params)
        return result.get("data", {}).get("songs", [{}])[0].get("id")

    async def _get_music_url(self, song_id: str) -> str | None:
        params = {"token": self.token, "id": song_id}
        if self.cookie:
            params["cookie"] = self.cookie
        result = await self.api_request("https://v3.alapi.cn/api/music/url", params)
        return result.get("data", {}).get("url")

    async def __del__(self):
        await self._client.aclose()
