"""RTSP 攝影機擷取：每隻攝影機一個背景執行緒，只保留最新影格，斷線自動重連。"""

import logging
import os
import threading
import time

# 強制 RTSP 走 TCP，避免 UDP 掉封包造成花屏；必須在 import cv2 之前設定
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402

log = logging.getLogger(__name__)


class RTSPCamera:
    def __init__(self, name, url, initial_delay=1.0, max_delay=30.0, timeout_ms=5000):
        self.name = name
        self.url = url
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.timeout_ms = timeout_ms

        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"cam-{name}", daemon=True)

        self.connected = False
        self.fps = 0.0

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self.timeout_ms / 1000 + 2)

    def read(self):
        """回傳 (frame, 影格時間戳)；尚未取得影像時 frame 為 None。"""
        with self._lock:
            if self._frame is None:
                return None, 0.0
            return self._frame.copy(), self._frame_time

    def _open(self):
        cap = cv2.VideoCapture(
            self.url,
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout_ms,
             cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout_ms],
        )
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _run(self):
        delay = self.initial_delay
        while not self._stop.is_set():
            log.info("[%s] 連線中...", self.name)
            cap = self._open()
            if cap is None:
                log.warning("[%s] 連線失敗，%.1f 秒後重試", self.name, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, self.max_delay)
                continue

            log.info("[%s] 已連線", self.name)
            self.connected = True
            delay = self.initial_delay
            frames, t0 = 0, time.time()

            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    log.warning("[%s] 讀取影格失敗，準備重連", self.name)
                    break
                now = time.time()
                with self._lock:
                    self._frame = frame
                    self._frame_time = now

                frames += 1
                if now - t0 >= 1.0:
                    self.fps = frames / (now - t0)
                    frames, t0 = 0, now

            self.connected = False
            self.fps = 0.0
            cap.release()
