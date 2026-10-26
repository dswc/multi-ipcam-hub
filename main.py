"""multi-ipcam-hub：同時連接多隻 RTSP IPCam，以分割畫面顯示，可截圖與錄影。

按鍵：
    q / ESC  離開
    s        每隻攝影機各存一張截圖
    r        開始 / 停止錄影
"""

import argparse
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import cv2
import numpy as np
import yaml

from camera import RTSPCamera

WINDOW = "multi-ipcam-hub"
STALE_SECONDS = 3.0  # 超過此秒數沒有新影格，視為訊號中斷


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_url(cam):
    """有 url 就直接使用，否則由 host/port/username/password/path 組成 RTSP URL。"""
    if cam.get("url"):
        return cam["url"]
    if not cam.get("password"):
        logging.warning("[%s] 尚未設定密碼，請編輯設定檔的 password 欄位", cam["name"])
    auth = ""
    if cam.get("username"):
        auth = quote(str(cam["username"]), safe="")
        if cam.get("password"):
            auth += ":" + quote(str(cam["password"]), safe="")
        auth += "@"
    path = "/" + str(cam.get("path", "")).lstrip("/")
    return f"rtsp://{auth}{cam['host']}:{cam.get('port', 554)}{path}"


def placeholder(width, height, text=""):
    img = np.zeros((height, width, 3), dtype=np.uint8)
    if text:
        cv2.putText(img, text, (20, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
    return img


def fit_to_cell(frame, width, height, keep_aspect):
    """把影格縮放成 width x height；keep_aspect 時等比縮放並置中，四周補黑邊。"""
    if not keep_aspect:
        return cv2.resize(frame, (width, height))
    h, w = frame.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    cell = np.zeros((height, width, 3), dtype=np.uint8)
    x, y = (width - nw) // 2, (height - nh) // 2
    cell[y:y + nh, x:x + nw] = cv2.resize(frame, (nw, nh))
    return cell


def compose_grid(cells, columns, rows, width, height):
    """依序把各格排成 columns x rows，不足的格子補黑色。"""
    cells = cells + [placeholder(width, height)] * (columns * rows - len(cells))
    return np.vstack([np.hstack(cells[r * columns:(r + 1) * columns]) for r in range(rows)])


def draw_label(img, text, color=(0, 255, 0)):
    cv2.putText(img, text, (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
    cv2.putText(img, text, (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)


def draw_bottom_center(img, text, color=(255, 255, 255)):
    (tw, _), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 4)
    org = ((img.shape[1] - tw) // 2, img.shape[0] - 10 - baseline)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class Recorder:
    """每隻攝影機各寫一個 mp4 檔。"""

    def __init__(self, output_dir, fps):
        self.output_dir = Path(output_dir)
        self.fps = fps
        self.writers = {}

    @property
    def active(self):
        return bool(self.writers)

    def start(self, names):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stamp = timestamp()
        self.writers = {name: None for name in names}  # 收到第一張影格時才建立，才知道解析度

    def write(self, name, frame):
        if name not in self.writers:
            return
        writer = self.writers[name]
        if writer is None:
            h, w = frame.shape[:2]
            path = self.output_dir / f"{name}_{self.stamp}.mp4"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
            self.writers[name] = writer
            logging.info("開始錄影：%s", path)
        writer.write(frame)

    def stop(self):
        for writer in self.writers.values():
            if writer is not None:
                writer.release()
        self.writers = {}
        logging.info("停止錄影")


def main():
    parser = argparse.ArgumentParser(description="multi-ipcam-hub：多 RTSP IPCam 檢視器")
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    display = cfg["display"]
    cell_w = display.get("cell_width", 854)
    cell_h = display.get("cell_height", 480)
    keep_aspect = display.get("keep_aspect", True)
    show_fps = display.get("show_fps", True)
    reconnect = cfg.get("reconnect", {})

    cam_cfgs = cfg["cameras"]
    columns = display.get("columns", len(cam_cfgs))
    rows = display.get("rows", math.ceil(len(cam_cfgs) / columns))
    if len(cam_cfgs) > columns * rows:
        logging.warning("攝影機有 %d 隻，但畫面只有 %dx%d=%d 格，只顯示前 %d 隻",
                        len(cam_cfgs), columns, rows, columns * rows, columns * rows)
        cam_cfgs = cam_cfgs[:columns * rows]

    cams = [
        RTSPCamera(c["name"], build_url(c), **reconnect).start()
        for c in cam_cfgs
    ]
    # 用 url 設定的攝影機沒有 host 欄位，改從 URL 解析出 IP
    hosts = {c["name"]: c.get("host") or urlsplit(build_url(c)).hostname or "" for c in cam_cfgs}
    recorder = Recorder(cfg["recording"]["output_dir"], cfg["recording"]["fps"])
    snapshot_dir = Path(cfg.get("snapshot_dir", "snapshots"))
    record_interval = 1.0 / cfg["recording"]["fps"]
    last_record = 0.0

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, columns * cell_w, rows * cell_h)
    try:
        while True:
            now = time.time()
            raw_frames, tiles = {}, []
            for cam in cams:
                frame, ts = cam.read()
                if frame is None:
                    tile = placeholder(cell_w, cell_h, f"{cam.name}: connecting...")
                    draw_bottom_center(tile, hosts[cam.name])
                    tiles.append(tile)
                    continue
                raw_frames[cam.name] = frame
                tile = fit_to_cell(frame, cell_w, cell_h, keep_aspect)
                stale = now - ts > STALE_SECONDS
                label = cam.name + (f"  {cam.fps:.1f} fps" if show_fps else "")
                draw_label(tile, label + ("  [NO SIGNAL]" if stale else ""),
                           (0, 0, 255) if stale else (0, 255, 0))
                draw_bottom_center(tile, hosts[cam.name])
                tiles.append(tile)

            if recorder.active and now - last_record >= record_interval:
                for name, frame in raw_frames.items():
                    recorder.write(name, frame)
                last_record = now

            view = compose_grid(tiles, columns, rows, cell_w, cell_h)
            if recorder.active:
                cv2.circle(view, (view.shape[1] - 25, 25), 10, (0, 0, 255), -1)
            # WINDOW_NORMAL 會把畫面拉伸成視窗形狀；使用者拖曳改變視窗大小時，先等比縮放補黑邊再顯示
            _, _, win_w, win_h = cv2.getWindowImageRect(WINDOW)
            if keep_aspect and win_w > 0 and win_h > 0 and (win_w, win_h) != (view.shape[1], view.shape[0]):
                view = fit_to_cell(view, win_w, win_h, True)
            cv2.imshow(WINDOW, view)

            key = cv2.waitKey(10) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("s"):
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                stamp = timestamp()
                for name, frame in raw_frames.items():
                    path = snapshot_dir / f"{name}_{stamp}.jpg"
                    cv2.imwrite(str(path), frame)
                    logging.info("截圖：%s", path)
            if key == ord("r"):
                if recorder.active:
                    recorder.stop()
                else:
                    recorder.start([c.name for c in cams])
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        if recorder.active:
            recorder.stop()
        for cam in cams:
            cam.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
