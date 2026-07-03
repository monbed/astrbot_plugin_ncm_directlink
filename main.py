"""AstrBot 网易云音乐插件：搜索歌曲后按需获取直链或直接发送音乐文件。

交互流程（均只对发起搜索的用户生效，逐步超时 60 秒）：
1. `/下载音乐 <歌名>` → 返回搜索结果列表；
2. 回复序号选歌 → 返回发送方式菜单；
3. 回复 1=直链 / 2=文件（0=取消）。文件发送前先按「歌名 - 歌手.格式」重命名。
"""

import asyncio
import os
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

SELECT_TIMEOUT_SECONDS = 60
"""每一步等待用户回复序号的有效期（秒）"""
CACHE_JANITOR_SECONDS = 300
"""兜底清理阈值：超过该时长的会话缓存在任意消息到来时被清除"""
DOWNLOAD_TIMEOUT_SECONDS = 300.0
"""音乐文件下载超时（独立于 API 请求超时，无损格式体积大）"""
STALE_FILE_SECONDS = 3600
"""下载目录中残留文件（发送中断未清理）的回收阈值"""

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class DownloadMusicPlugin(Star):
    """网易云音乐直链/文件发送工具。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # AstrBotConfig 不做类型校验，且完整性检查会原样保留空字符串，
        # 故空值不合法的配置一律用 or 兜底。
        self.apiurl = (config.get("apiurl") or "").rstrip("/")
        self.level = config.get("level") or "jyeffect"
        self.cookie = config.get("cookie") or ""
        self.limit = self._safe_int(config.get("limit"), 10)
        self.timeout = self._safe_float(config.get("timeout"), 10.0)

        self._lock = asyncio.Lock()
        self._last_req = 0.0
        self._search_cache: dict[str, dict] = {}
        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        self._download_dir = (
            StarTools.get_data_dir("astrbot_plugin_ncm_directlink") / "downloads"
        )
        self._download_dir.mkdir(parents=True, exist_ok=True)

        if not self.apiurl:
            logger.warning("[ncm_directlink] 未配置 apiurl，插件功能不可用。")

    async def terminate(self):
        """插件停用/重载时关闭共享的 httpx 客户端。"""
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # 工具函数
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            n = int(value)
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value, default: float) -> float:
        try:
            f = float(value)
            return f if f > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_song(song: dict) -> str:
        """格式化歌曲信息为 '歌名 - 歌手 [专辑]'"""
        name = song.get("name", "未知")
        artist = ", ".join(a.get("name", "") for a in song.get("ar", []))
        album = song.get("al", {}).get("name", "")
        return f"{name} - {artist} [{album}]"

    @staticmethod
    def _cache_key(event: AstrMessageEvent) -> str:
        # unified_msg_origin 在群聊里是整群共享的，必须再叠加发送者 ID，
        # 否则群内多人同时点歌会互相覆盖缓存。
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        name = _ILLEGAL_FILENAME_CHARS.sub("_", name).strip(" .")
        return name[:120] or "song"

    # ------------------------------------------------------------------ #
    # API 请求
    # ------------------------------------------------------------------ #
    async def api_request(self, url: str, params: dict) -> dict:
        async with self._lock:
            wait = 1.0 - (time.time() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            self._last_req = time.time()
            return resp.json()

    async def _get_musicids(self, keyword: str) -> list[dict]:
        params = {"keywords": keyword, "type": 1, "limit": self.limit}
        result = await self.api_request(f"{self.apiurl}/cloudsearch", params)
        songs = (
            (result.get("result") or {}).get("songs")
            if isinstance(result, dict)
            else None
        )
        return songs or []

    async def _get_download_info(self, song_id) -> dict | None:
        """取歌曲下载信息，返回 API 的 data 字段（含 url/type/size 等）。"""
        params = {"id": song_id, "level": self.level}
        if self.cookie:
            params["cookie"] = self.cookie
        try:
            result = await self.api_request(f"{self.apiurl}/song/download/url/v1", params)
        except Exception as e:
            logger.error(f"[ncm_directlink] 获取下载信息异常: {e}")
            return None
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ #
    # 文件下载
    # ------------------------------------------------------------------ #
    def _sweep_stale_downloads(self) -> None:
        """回收发送中断遗留的旧文件。"""
        now = time.time()
        try:
            for f in self._download_dir.iterdir():
                try:
                    if f.is_file() and now - f.stat().st_mtime > STALE_FILE_SECONDS:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    async def _download_song(self, song: dict, data: dict) -> Path:
        """流式下载歌曲，先按「歌名 - 歌手.格式」重命名，再交给发送。"""
        self._sweep_stale_downloads()
        url = data["url"]
        ext = (data.get("type") or "").strip().lstrip(".").lower()
        if not ext:
            ext = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower() or "mp3"
        name = song.get("name", "未知")
        artist = ", ".join(a.get("name", "") for a in song.get("ar", []))
        stem = self._sanitize_filename(f"{name} - {artist}" if artist else name)
        target = self._download_dir / f"{stem}.{ext}"

        tmp = self._download_dir / f".{uuid.uuid4().hex}.part"
        try:
            async with self._client.stream(
                "GET", url, timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
            ) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        f.write(chunk)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return target

    # ------------------------------------------------------------------ #
    # 指令与序号分发
    # ------------------------------------------------------------------ #
    @filter.command("下载音乐")
    async def download_music(self, event: AstrMessageEvent, music_name: GreedyStr):
        """搜索网易云音乐，回复序号后可选择获取直链或发送文件。"""
        if not self.apiurl:
            yield event.plain_result("❌ 插件未配置 apiurl，请在管理面板的插件配置中填写。")
            return
        keyword = str(music_name or "").strip()
        if not keyword:
            yield event.plain_result("用法：下载音乐 <歌名>")
            return

        try:
            songs = await self._get_musicids(keyword)
        except Exception as e:
            logger.error(f"[ncm_directlink] 搜索异常: {e}", exc_info=True)
            yield event.plain_result("❌ 搜索歌曲时发生异常，请稍后重试")
            return
        if not songs:
            yield event.plain_result(f"❌ 未找到歌曲「{keyword}」")
            return

        lines = ["搜索结果："]
        for i, song in enumerate(songs, 1):
            lines.append(f"{i}. {self._format_song(song)}")
        lines.append(
            f"请回复序号选择歌曲（{SELECT_TIMEOUT_SECONDS} 秒内有效，回复 0 取消）"
        )
        self._search_cache[self._cache_key(event)] = {
            "stage": "pick_song",
            "songs": songs,
            "song": None,
            "ts": time.time(),
        }
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_selection(self, event: AstrMessageEvent):
        """处理选歌/选发送方式的序号回复（仅对发起搜索的用户生效）。"""
        now = time.time()
        if self._search_cache:
            expired = [
                k
                for k, v in self._search_cache.items()
                if now - v["ts"] > CACHE_JANITOR_SECONDS
            ]
            for k in expired:
                self._search_cache.pop(k, None)

        key = self._cache_key(event)
        cache = self._search_cache.get(key)
        if not cache:
            return

        text = event.message_str.strip()
        if not text.isdigit():
            # 非序号消息不拦截，放行给其他插件 / LLM
            return

        if now - cache["ts"] > SELECT_TIMEOUT_SECONDS:
            self._search_cache.pop(key, None)
            yield event.plain_result("❌ 回复超时，请重新发送指令")
            event.stop_event()
            return

        idx = int(text)
        if idx == 0:
            self._search_cache.pop(key, None)
            yield event.plain_result("已取消")
            event.stop_event()
            return

        if cache["stage"] == "pick_song":
            songs = cache["songs"]
            if not (1 <= idx <= len(songs)):
                return  # 超范围数字视为普通聊天，不拦截
            cache["song"] = songs[idx - 1]
            cache["stage"] = "pick_delivery"
            cache["ts"] = now
            yield event.plain_result(
                f"已选择：{self._format_song(cache['song'])}\n"
                f"请回复发送方式：1. 直链  2. 文件"
                f"（{SELECT_TIMEOUT_SECONDS} 秒内有效，回复 0 取消）"
            )
            event.stop_event()
            return

        # stage == "pick_delivery"
        if idx not in (1, 2):
            return
        song = cache["song"]
        self._search_cache.pop(key, None)  # 进入执行即销毁，防止重复触发
        info = self._format_song(song)

        data = await self._get_download_info(song.get("id"))
        url = (data or {}).get("url")
        if not url:
            yield event.plain_result(
                f"❌ {info}\n获取直链失败（可能需要有效 cookie 或降低音质等级）"
            )
            event.stop_event()
            return

        if idx == 1:
            yield event.plain_result(f"✅ {info}\n直链：{url}")
            event.stop_event()
            return

        # idx == 2：发送文件。先下载并按歌名重命名，再发送。
        yield event.plain_result(f"⏳ 正在下载「{info}」，请稍候…")
        try:
            file_path = await self._download_song(song, data)
        except Exception as e:
            logger.error(f"[ncm_directlink] 下载失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 文件下载失败：{e}\n直链：{url}")
            event.stop_event()
            return

        try:
            yield event.chain_result(
                [File(name=file_path.name, file=str(file_path))]
            )
        finally:
            # 洋葱模型保证 yield 返回时发送已完成，可安全清理本地文件
            try:
                file_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"[ncm_directlink] 清理下载文件失败: {e}")
        event.stop_event()
