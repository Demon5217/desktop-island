# -*- coding: utf-8 -*-
"""Windows 媒体会话（SMTC）采集与控制。

系统级接口，能读到任何遵守 SMTC 的播放器（网易云、QQ音乐、Spotify、浏览器等）。
读取：歌名 / 歌手 / 专辑 / 封面 / 播放状态 / 进度
控制：播放暂停 / 上一首 / 下一首
"""
from __future__ import annotations

import ctypes
import threading
import time

from PySide6.QtCore import QThread, Signal

APP_NAMES = {
    "msedge": "Edge",
    "chrome": "Chrome",
    "spotify": "Spotify",
    "cloudmusic": "网易云音乐",
    "qqmusic": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "potplayer": "PotPlayer",
    "vlc": "VLC",
    "musicbee": "MusicBee",
    "foobar2000": "foobar2000",
    "aimp": "AIMP",
}


STATUS_NAMES = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
    -1: "unknown",
}


def _friendly_app(aumid: str) -> str:
    low = (aumid or "").lower()
    for key, name in APP_NAMES.items():
        if key in low:
            return name
    base = (aumid or "").split("!")[0]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base or "媒体"


class MediaWorker(QThread):
    updated = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()
        self._mgr = None
        self._cover_cache_title = None
        self._cover_bytes = None
        self._lock = threading.Lock()
        self._actions: list[str] = []
        self._session = None

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # 多线程套间
        except Exception:
            pass

        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )

            self._mgr = Manager.request_async().get()
        except Exception as e:
            self.updated.emit({"has_session": False, "error": f"SMTC 不可用：{e}"})
            return

        while not self._stop.is_set():
            try:
                self._poll()
                self._run_actions()
            except Exception:
                pass
            self.msleep(1000)

        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    def post(self, action: str) -> None:
        with self._lock:
            self._actions.append(action)

    # ------------------------------------------------------------------
    def _run_actions(self) -> None:
        with self._lock:
            actions = self._actions
            self._actions = []
        sess = self._session
        if sess is None:
            return
        for act in actions:
            try:
                if act == "toggle":
                    sess.try_toggle_play_pause_async().get()
                elif act == "next":
                    sess.try_skip_next_async().get()
                elif act == "prev":
                    sess.try_skip_previous_async().get()
            except Exception:
                pass

    def _poll(self) -> None:
        if self._mgr is None:
            return
        sess = self._mgr.get_current_session()
        self._session = sess
        if sess is None:
            if self._cover_cache_title is not None:
                self._cover_cache_title = None
                self._cover_bytes = None
            self.updated.emit({"has_session": False})
            return

        info = sess.try_get_media_properties_async().get()
        title = (info.title or "").strip() or "未知曲目"
        artist = (info.artist or "").strip()
        album = (info.album_title or "").strip()

        playing = False
        status_name = ""
        try:
            # winrt 新版没有 get_playback_status()，得从 playback_info 里取
            pb = sess.get_playback_info()
            code = int(pb.playback_status.value)
            status_name = STATUS_NAMES.get(code, str(code))
            playing = code == 4
        except Exception:
            pass

        pos, dur = 0.0, 0.0
        try:
            tl = sess.get_timeline_properties()
            pos = tl.position.total_seconds()
            dur = tl.end_time.total_seconds()
            if dur < 0:
                dur = 0.0
        except Exception:
            pass

        app = _friendly_app(sess.source_app_user_model_id)

        if title != self._cover_cache_title:
            self._cover_cache_title = title
            self._cover_bytes = self._read_cover(info)

        self.updated.emit(
            {
                "has_session": True,
                "title": title,
                "artist": artist,
                "album": album,
                "playing": playing,
                "status": status_name,
                "position": pos,
                "duration": dur,
                "app": app,
                "aumid": sess.source_app_user_model_id or "",
                "cover": self._cover_bytes,
            }
        )

    def _read_cover(self, info) -> bytes | None:
        """封面是异步流，读失败就返回 None（界面会画个占位块）。"""
        try:
            thumb = info.thumbnail
            if thumb is None:
                return None
            stream = thumb.open_read_async().get()
            if stream is None:
                return None
            from winrt.windows.storage.streams import DataReader

            size = int(stream.size)
            if size <= 0 or size > 8 * 1024 * 1024:
                return None
            reader = DataReader(stream)
            reader.load_async(size).get()
            buf = reader.read_buffer(size)
            data = bytes(buf)
            if data:
                return data
        except Exception:
            return None
        return None
