# -*- coding: utf-8 -*-
"""音频：WASAPI loopback 频谱（律动器）+ 按进程音量控制。

AudioWorker  —— 抓系统输出（不是麦克风），算 32 段对数频谱，25fps 发出去。
VolumeWorker —— 用 pycaw 调某一个播放器进程的会话音量，不动系统总音量。

两个线程分开是有原因的：soundcard 走 cffi 的 COM，pycaw/comtypes 需要 STA，
放进同一个线程会因为「设置线程模式后无法更改」打架。
"""
from __future__ import annotations

import threading

import numpy as np
from PySide6.QtCore import QThread, Signal

RATE = 48000
CHUNK = 1920          # 40ms @48k
BANDS = 32
F_LO = 50.0
F_HI = 16000.0
FPS = 25


class AudioWorker(QThread):
    """系统输出频谱。一直跑，没声音时自己降速，占 CPU 很低。"""

    spectrum = Signal(list, float)   # 32 个 0~1 的频带, 当前能量 0~1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()
        self._bands = [0.0] * BANDS
        edges = np.logspace(np.log10(F_LO), np.log10(F_HI), BANDS + 1)
        freqs = np.fft.rfftfreq(CHUNK, 1.0 / RATE)
        self._masks = [((freqs >= lo) & (freqs < hi)) for lo, hi in zip(edges[:-1], edges[1:])]
        self._win = np.hanning(CHUNK)
        self._quiet_frames = 0
        self._peak = np.full(BANDS, 1e-6, dtype=np.float32)
        self.level = 0.0

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            import soundcard as sc
        except Exception as e:
            self.spectrum.emit([0.0] * BANDS, 0.0)
            print("[audio] soundcard 不可用:", e)
            return

        while not self._stop.is_set():
            try:
                spk = sc.default_speaker()
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                with mic.recorder(samplerate=RATE, channels=2, blocksize=CHUNK) as rec:
                    while not self._stop.is_set():
                        data = rec.record(numframes=CHUNK)
                        self._tick(data)
                        # record() 本身已经按 40ms 阻塞了，这里千万别再 sleep：
                        # 多睡一帧会让采集节奏和 WASAPI 缓冲区错位，采到的是空数据
                        if self._quiet_frames:
                            self.msleep(60)
            except Exception as e:
                # 换设备 / 独占模式被占，等一会儿重开
                self.spectrum.emit([0.0] * BANDS, 0.0)
                print("[audio] loopback 重连:", e)
                self.msleep(1500)

    def _tick(self, data) -> None:
        mono = data.mean(axis=1).astype(np.float32)
        rms = float(np.sqrt(np.mean(mono ** 2)))
        self.level = rms

        if rms < 0.00015:
            # 单帧读到空数据很常见（WASAPI 缓冲还没跟上），连续几帧才算真的静音
            self._quiet_frames += 1
            if self._quiet_frames >= 6:
                self._bands = [b * 0.9 for b in self._bands]
                self.spectrum.emit(list(self._bands), 0.0)
            return

        self._quiet_frames = 0
        spec = np.abs(np.fft.rfft(mono * self._win))
        band = np.array([float(spec[m].mean()) if m.any() else 0.0 for m in self._masks])

        # 每根柱子各按自己的长时峰值归一（~4 秒半衰）。只按整帧最大值归一时，
        # 音乐的 90% 能量都堆在低频，高频那半屏会永远趴在地板上
        self._peak = np.maximum(band, self._peak * 0.97)
        ratio = band / np.maximum(self._peak, 1e-5)
        target = np.clip(ratio, 0.0, 1.0) ** 0.4
        # 整体安静时压低，避免把小噪声也打成满格（小音量下 RMS 只有千分之一量级，阈值要跟着低）
        target *= min(1.0, rms / 0.0006)

        # 上升立刻跟，下降慢一点，看着才像"跳"而不是噪点
        prev = np.array(self._bands)
        sm = np.maximum(target, prev * 0.88)
        self._bands = [round(float(v), 3) for v in sm]
        self.spectrum.emit(list(self._bands), min(1.0, rms / 0.02))


class VolumeWorker(QThread):
    """按进程名（子串匹配）调会话音量，不动系统总音量。

    匹配不到任何会话（比如 UWP 播放器）时回退成调系统主音量，至少不让滑块变成摆设。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._cmds: list = []
        self._stop = threading.Event()
        self.last = {}          # name -> 0~1 最近一次写入的值
        self.hit_process = {}   # name -> 是否真的改到了那个进程

    def stop(self) -> None:
        self._stop.set()

    def set_volume(self, name_sub: str, value: float) -> None:
        with self._lock:
            self._cmds.append(("vol", name_sub.lower(), max(0.0, min(1.0, value))))

    def set_mute(self, name_sub: str, muted: bool) -> None:
        with self._lock:
            self._cmds.append(("mute", name_sub.lower(), bool(muted)))

    def push_read(self, name_sub: str) -> None:
        """把当前音量读回来一次（第一次显示滑块用）。"""
        with self._lock:
            self._cmds.append(("read", name_sub.lower(), None))

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            from pycaw.pycaw import AudioUtilities
        except Exception as e:
            print("[audio] pycaw 不可用，音量控制关闭:", e)
            return

        while not self._stop.is_set():
            with self._lock:
                cmds = self._cmds
                self._cmds = []
            for kind, name, val in cmds:
                matched = False
                try:
                    for s in AudioUtilities.GetAllSessions():
                        proc = s.Process
                        if proc is None or name not in proc.name().lower():
                            continue
                        vol = s.SimpleAudioVolume
                        if kind == "vol":
                            vol.SetMasterVolume(float(val), None)
                            self.last[name] = float(val)
                        elif kind == "mute":
                            vol.SetMute(1 if val else 0, None)
                        elif kind == "read":
                            try:
                                self.last[name] = float(vol.GetMasterVolume())
                            except Exception:
                                pass
                        matched = True
                        break
                except Exception:
                    pass
                if kind == "vol" and not matched:
                    # 回退：没有匹配的会话就调系统主音量，别让滑块白拖
                    try:
                        AudioUtilities.GetSpeakers().EndpointVolume.SetMasterVolume(float(val), None)
                        self.last[name] = float(val)
                    except Exception:
                        pass
                self.hit_process[name] = matched
            self.msleep(120)
