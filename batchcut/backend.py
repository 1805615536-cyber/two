#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BatchCut Studio - 切影工坊 v3（对齐 AI 素材双轨路线图·项目A）

新增能力：
  AI 混剪      一批原生素材 -> 随机分段 + 乱序重组，一稿出 N 条（plan_remix）
  AI 前贴      片头/片尾贴片自动拼接（任务在创建时统一编译为「片段序列」）
  AI 换元素    每条成片随机：镜像/边缘裁切/微变速/调色预设/标题样式/水印位置/BGM
  素材库       资产新增 原生素材(video)/片头(intro) 类型 + 标签与脚本备注字段

分层：HTTP(Handler) -> 服务(TaskService / plan_remix) -> 执行(build_segments_command) -> 存储(StateStore)
"""
from __future__ import annotations

import json
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.path.join(APP_DIR, "data")
UP_DIR = os.path.join(DATA_DIR, "uploads")
OUT_DIR = os.path.join(DATA_DIR, "output")
POSTER_DIR = os.path.join(DATA_DIR, "posters")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

DEFAULT_SETTINGS = {
    "ffmpeg_path": os.path.join(APP_DIR, "tools", "ffmpeg.exe"),
    "font_path": "C:/Windows/Fonts/msyh.ttc",
    "concurrency": 2,
    "crf": 23,
    "preset": "veryfast",
}

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".flv", ".wmv", ".webm", ".ts", ".mts"}
ASSET_KINDS = ("bgm", "watermark", "video", "intro", "other")
ASSET_KIND_LABEL = {"bgm": "配乐", "watermark": "水印", "video": "原生素材",
                    "intro": "片头/贴片", "other": "素材"}

for d in (DATA_DIR, UP_DIR, OUT_DIR, POSTER_DIR):
    os.makedirs(d, exist_ok=True)


def now_ts() -> float:
    return time.time()


def sanitize(name: str, fallback: str = "output") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    name = name.strip(". ")
    return name[:80] or fallback


def run_hidden(args, **kw):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags, **kw)


# --------------------------------------------------------------------------------------
# 存储层
# --------------------------------------------------------------------------------------
class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"tasks": [], "settings": dict(DEFAULT_SETTINGS), "seq": 1,
                     "users": [], "assets": [], "projects": [], "oplog": []}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data["tasks"] = loaded.get("tasks", [])
                s = dict(DEFAULT_SETTINGS)
                s.update(loaded.get("settings", {}))
                self.data["settings"] = s
                self.data["seq"] = loaded.get("seq", 1)
                for k in ("users", "assets", "projects", "oplog"):
                    self.data[k] = loaded.get(k) or []
            except Exception:
                traceback.print_exc()
        for t in self.data["tasks"]:
            if t.get("status") == "processing":
                t.update(status="queued", progress=0, stage="服务重启，重新排队", eta_sec=None)
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def next_seq(self) -> int:
        with self.lock:
            n = self.data["seq"]
            self.data["seq"] = n + 1
            return n


STORE = StateStore(STATE_FILE)

if not STORE.data["users"]:
    STORE.data["users"] = [{"id": uuid.uuid4().hex[:8], "name": "管理员", "role": "管理员",
                            "note": "默认账号", "created_at": now_ts()}]
    STORE.save()


def log_op(action, detail="", user=""):
    with STORE.lock:
        oplog = STORE.data.setdefault("oplog", [])
        oplog.append({"ts": now_ts(), "user": user or "系统",
                      "action": action, "detail": str(detail)[:300]})
        STORE.data["oplog"] = oplog[-500:]


# --------------------------------------------------------------------------------------
# 执行层
# --------------------------------------------------------------------------------------
def ffmpeg_bin() -> str:
    p = STORE.data["settings"].get("ffmpeg_path") or ""
    if p and os.path.exists(p):
        return p
    return shutil.which("ffmpeg") or "ffmpeg"


_PROBE_CACHE = {}
_PROBE_LOCK = threading.Lock()


def probe(path: str, use_cache=True) -> dict:
    """ffmpeg -i 读取时长/分辨率/音轨（带缓存：混剪规划需大量探测）。"""
    if use_cache:
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(path)
        if hit:
            return hit
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "fps": 0.0}
    try:
        p = run_hidden([ffmpeg_bin(), "-hide_banner", "-i", path])
        out, _ = p.communicate(timeout=120)
        text = (out or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", text)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
        if m:
            info["width"], info["height"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"([\d.]+)\s*fps", text)
        if m:
            info["fps"] = float(m.group(1))
        info["has_audio"] = "Audio:" in text
    except Exception:
        traceback.print_exc()
    if use_cache:
        with _PROBE_LOCK:
            _PROBE_CACHE[path] = info
    return info


_POSTER_LOCK = threading.Lock()
_POSTER_BUSY = set()


def poster_for(fp: str) -> str:
    """为视频生成一张封面图（列表/审核卡片用），按 路径+修改时间 缓存。"""
    try:
        key = hashlib.md5(("%s|%s" % (fp, os.path.getmtime(fp))).encode("utf-8")).hexdigest()
    except Exception:
        return ""
    dst = os.path.join(POSTER_DIR, key + ".jpg")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    with _POSTER_LOCK:
        if key in _POSTER_BUSY:
            return ""                      # 别的请求正在生成，让前端稍后重试
        _POSTER_BUSY.add(key)
    # 临时文件必须以 .jpg 结尾（ffmpeg 按扩展名推断输出格式）
    tmp = os.path.join(POSTER_DIR, ".tmp_%s.jpg" % uuid.uuid4().hex[:8])
    try:
        info = probe(fp)
        ss = min(2.0, max(0.0, (info.get("duration") or 2.0) * 0.25))
        p = run_hidden([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", "%.2f" % ss, "-i", fp, "-frames:v", "1",
                        "-vf", "scale=400:-2", "-q:v", "4", tmp])
        p.communicate(timeout=90)
        if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return dst
    except Exception:
        traceback.print_exc()
    finally:
        with _POSTER_LOCK:
            _POSTER_BUSY.discard(key)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    return ""


def atempo_chain(speed: float) -> str:
    if abs(speed - 1.0) < 1e-3:
        return ""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append("atempo=%.4f" % s)
    return ",".join(parts)


def esc_drawtext(text: str) -> str:
    t = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")
    return t.replace("%", "\\%").replace(",", "\\,")


def target_size(src: dict, ops: dict):
    """输出画布：size=source 用素材原尺寸（偶数化），旋转 90/270 时宽高互换。"""
    size = (ops.get("size") or "source").lower()
    if size != "source":
        try:
            tw, th = [int(x) for x in size.split("x")]
            if tw > 1 and th > 1:
                return tw, th
        except Exception:
            pass
    w = int((src or {}).get("width") or 1080)
    h = int((src or {}).get("height") or 1920)
    if int(ops.get("rotate") or 0) % 360 in (90, 270):
        w, h = h, w
    return max(2, w - w % 2), max(2, h - h % 2)


FIT_MODES = ("blur", "pad", "cover")


def crop_filter(ops: dict) -> str:
    """四边裁切，参数为百分比（0–45，表示裁掉该边多少）。"""
    c = ops.get("crop") or {}
    try:
        t, b, l, r = [max(0.0, min(45.0, float(c.get(k) or 0)))
                      for k in ("top", "bottom", "left", "right")]
    except Exception:
        return ""
    if not (t or b or l or r) or l + r >= 90 or t + b >= 90:
        return ""
    return "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f" % (
        (100.0 - l - r) / 100.0, (100.0 - t - b) / 100.0, l / 100.0, t / 100.0)


def rotate_filter(ops: dict) -> str:
    rot = int(ops.get("rotate") or 0) % 360
    return {90: "transpose=1", 180: "transpose=1,transpose=1",
            270: "transpose=2"}.get(rot, "")


def title_filter(ops: dict, st: dict) -> str:
    """标题字幕（花字库简化版：4 种样式）。"""
    title = (ops.get("title") or "").strip()
    if not title:
        return ""
    font = (st.get("font_path") or "C:/Windows/Fonts/msyh.ttc").replace("\\", "/").replace(":", "\\:")
    fsize = int(ops.get("title_size") or 44)
    margin = int(ops.get("title_margin") or 90)
    y = "h-text_h-%d" % margin if (ops.get("title_pos") or "bottom") == "bottom" else "%d" % max(10, margin)
    base = "fontfile='%s':text='%s':fontsize=%d:x=(w-text_w)/2:y=%s" % (
        font, esc_drawtext(title), fsize, y)
    style = ops.get("title_style") or "box"
    if style == "yellow":
        return "drawtext=" + base + ":fontcolor=#FFD400:borderw=4:bordercolor=black@0.9"
    if style == "shadow":
        return "drawtext=" + base + ":fontcolor=white:shadowx=4:shadowy=4:shadowcolor=black@0.75"
    if style == "outline":
        return "drawtext=" + base + ":fontcolor=white:borderw=5:bordercolor=black@0.85"
    return "drawtext=" + base + ":fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=14"


def build_segments_command(task: dict, out_path: str):
    """统一执行入口：把任务的片段序列编译成一条 ffmpeg 命令。

    segments: [{path, start, dur, has_audio}]（创建时已规划，含片头/片尾贴片）。
    流水线：逐段归一化(缩放/补边/30fps) -> concat -> 调色/变速/标题 -> 水印；
    音频：无音轨段自动补静音 -> concat -> 配乐混音 -> 变速。
    """
    st = STORE.data["settings"]
    ops = task.get("ops") or {}
    segs = task.get("segments") or []
    if not segs:
        raise RuntimeError("任务没有可处理的片段")

    tw, th = target_size(task.get("src_info") or {}, ops)
    speed = min(4.0, max(0.25, float(ops.get("speed") or 1.0)))
    out_len = sum(s["dur"] for s in segs) / max(0.25, speed)

    # ---- 输入顺序：片段视频 -> 静音源 -> 配乐 -> 水印 ----
    args = [ffmpeg_bin(), "-hide_banner", "-y", "-loglevel", "error"]
    for s in segs:
        args += ["-ss", "%.3f" % s["start"], "-t", "%.3f" % s["dur"], "-i", s["path"]]
    base = len(segs)
    silent_ord = {}
    for i, s in enumerate(segs):
        if not s.get("has_audio"):
            silent_ord[i] = len(silent_ord)
            args += ["-f", "lavfi", "-t", "%.3f" % s["dur"],
                     "-i", "anullsrc=r=44100:cl=stereo"]
    cur = base + len(silent_ord)
    bgm = ops.get("bgm") or {}
    bgm_idx = None
    if bgm.get("path") and os.path.isfile(bgm["path"]):
        args += ["-stream_loop", "-1", "-i", bgm["path"]]
        bgm_idx = cur
        cur += 1
    wm = ops.get("watermark") or {}
    wm_idx = None
    if wm.get("path") and os.path.isfile(wm["path"]):
        args += ["-i", wm["path"]]
        wm_idx = cur
        cur += 1

    fc = []
    fit = (ops.get("fit") or "blur").lower()
    if fit not in FIT_MODES:
        fit = "blur"
    pre = [x for x in (crop_filter(ops), rotate_filter(ops)) if x] + ["fps=30"]
    pre_chain = ",".join(pre)

    # ① 段内预处理：裁切 -> 旋转 -> 定帧率
    for i in range(len(segs)):
        fc.append("[%d:v]%s[pn%d]" % (i, pre_chain, i))
    # ② 画布适配：blur 模糊背景 / pad 黑边 / cover 裁切填满
    for i in range(len(segs)):
        if fit == "cover":
            fc.append("[pn{i}]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                      "crop={tw}:{th},setsar=1,format=yuv420p[p{i}]".format(i=i, tw=tw, th=th))
        elif fit == "blur":
            bw, bh = max(16, tw // 8), max(16, th // 8)
            fc.append("[pn{i}]split=2[bg{i}][fg{i}]".format(i=i))
            # 背景先降采样做模糊（快），再放大回画布尺寸（overlay 以它为主输入，决定输出尺寸）
            fc.append("[bg{i}]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                      "crop={tw}:{th},scale={bw}:{bh},gblur=sigma=8,"
                      "eq=brightness=-0.06,scale={tw}:{th}[bgb{i}]"
                      .format(i=i, tw=tw, th=th, bw=bw, bh=bh))
            fc.append("[fg{i}]scale={tw}:{th}:force_original_aspect_ratio=decrease[fgs{i}]"
                      .format(i=i, tw=tw, th=th))
            fc.append("[bgb{i}][fgs{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[p{i}]"
                      .format(i=i))
        else:
            fc.append("[pn{i}]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                      "pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[p{i}]"
                      .format(i=i, tw=tw, th=th))
    # ③ 拼接
    fc.append("{pairs}concat=n={n}:v=1:a=1[vc][ac]".format(
        pairs="".join("[p{v}]{a}".format(v=i, a="[%d:a]" % i if segs[i].get("has_audio")
                       else "[%d:a]" % (base + silent_ord[i])) for i in range(len(segs))),
        n=len(segs)))

    # ---- 视频后处理：镜像/调色/变速/标题 + 水印 ----
    vf = []
    if ops.get("mirror"):
        vf.append("hflip")
    bright = float(ops.get("brightness") or 0)
    contrast = float(ops.get("contrast") or 0)
    sat = float(ops.get("saturation") or 0)
    if bright or contrast or sat:
        vf.append("eq=brightness=%.3f:contrast=%.3f:saturation=%.3f"
                  % (bright, 1.0 + contrast, 1.0 + sat))
    if abs(speed - 1.0) > 1e-3:
        vf.append("setpts=%.5f*PTS" % (1.0 / speed))
    tf = title_filter(ops, st)
    if tf:
        vf.append(tf)
    vchain = ",".join(vf) if vf else "null"

    vlabel = "vc"
    if wm_idx is not None:
        pct = max(0.03, min(0.6, float(wm.get("scale") or 0.16)))
        wm_w = max(16, int(round(tw * pct / 2.0)) * 2)
        opacity = max(0.05, min(1.0, float(wm.get("opacity") or 0.85)))
        pos = wm.get("position") or "topright"
        x = {"topleft": "24", "topright": "main_w-overlay_w-24",
             "bottomleft": "24", "bottomright": "main_w-overlay_w-24",
             "center": "(main_w-overlay_w)/2"}.get(pos, "main_w-overlay_w-24")
        y = {"topleft": "24", "topright": "24",
             "bottomleft": "main_h-overlay_h-24", "bottomright": "main_h-overlay_h-24",
             "center": "(main_h-overlay_h)/2"}.get(pos, "24")
        fc.append("[vc]split=2[vb1][vb2]")
        fc.append("[vb2][%d:v]scale=%d:-2,format=rgba,colorchannelmixer=aa=%.3f[wmk]"
                  % (wm_idx, wm_w, opacity))
        fc.append("[vb1]%s[vfx]" % vchain)
        fc.append("[vfx][wmk]overlay=%s:%s[vout]" % (x, y))
        vlabel = "vout"
    else:
        fc.append("[vc]%s[vout]" % vchain)
        vlabel = "vout"

    # ---- 音频后处理：配乐混音 + 变速 ----
    amap = "[ac]"
    if bgm_idx is not None:
        vol = max(0.0, min(2.0, float(bgm.get("volume") or 0.35)))
        fc.append("[%d:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                  "volume=%.3f[bg]" % (bgm_idx, vol))
        fc.append("[ac][bg]amix=inputs=2:duration=first:dropout_transition=0[am]")
        amap = "[am]"
    if abs(speed - 1.0) > 1e-3:
        fc.append("%s%s[aout]" % (amap, atempo_chain(speed)))
        amap = "[aout]"

    args += ["-filter_complex", ";".join(fc), "-map", "[%s]" % vlabel, "-map", amap]
    args += ["-t", "%.3f" % out_len,
             "-c:v", "libx264", "-preset", str(st.get("preset") or "veryfast"),
             "-crf", str(int(ops.get("crf") or st.get("crf") or 23)),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-progress", "pipe:1", "-nostats", out_path]
    return args, out_len


# --------------------------------------------------------------------------------------
# AI 混剪规划器（随机分段 + 乱序重组 + 换元素随机化）
# --------------------------------------------------------------------------------------
COLOR_PRESETS = [(0.02, 0.06, 0.08), (-0.02, 0.10, -0.06), (0.04, -0.04, 0.12),
                 (0.0, 0.04, 0.0), (0.01, 0.12, 0.05), (-0.03, 0.02, -0.10)]
TITLE_STYLES = ["box", "yellow", "shadow", "outline"]
WM_POSITIONS = ["topleft", "topright", "bottomleft", "bottomright"]


def plan_remix(files, remix, base_ops, naming):
    """把一批素材规划成 N 条混剪方案（每条 = 随机片段序列 + 随机化参数）。"""
    remix = remix or {}
    count = max(1, min(20, int(remix.get("count") or 3)))
    dmin = max(5.0, float(remix.get("dur_min") or 20))
    dmax = max(dmin + 1, float(remix.get("dur_max") or 35))
    seg_min = max(1.5, float(remix.get("seg_min") or 2.5))
    seg_max = max(seg_min + 0.5, float(remix.get("seg_max") or 6.5))
    titles = [t.strip() for t in (remix.get("titles") or []) if t and t.strip()]
    bgm_paths = [b for b in (remix.get("bgm_paths") or []) if b and os.path.isfile(b)]
    intro_path = remix.get("intro_path") if remix.get("intro_path") and os.path.isfile(remix["intro_path"]) else None
    outro_path = remix.get("outro_path") if remix.get("outro_path") and os.path.isfile(remix["outro_path"]) else None

    infos = {p: probe(p) for p in files if os.path.isfile(p)}
    usable = [p for p in files if p in infos and infos[p]["duration"] >= 1.2]
    if not usable:
        raise RuntimeError("没有时长足够的原生素材（每段素材需 ≥1.2 秒）")

    plans = []
    for _ in range(count):
        target = random.uniform(dmin, dmax)
        segs, total, guard = [], 0.0, 0
        while total < target and guard < 80:
            p = random.choice(usable)
            info = infos[p]
            want = min(random.uniform(seg_min, seg_max), target - total)
            if info["duration"] <= want + 0.4:
                start, dur = 0.0, max(1.0, info["duration"] - 0.2)
            else:
                start = random.uniform(0, info["duration"] - want - 0.2)
                dur = want
            segs.append({"path": p, "start": round(start, 2), "dur": round(dur, 2),
                         "has_audio": info["has_audio"]})
            total += dur
            guard += 1
        for is_intro, path in ((True, intro_path), (False, outro_path)):
            if path:
                info = probe(path)
                seg = {"path": path, "start": 0.0,
                       "dur": round(max(0.5, info["duration"]), 2),
                       "has_audio": info["has_audio"]}
                segs.insert(0, seg) if is_intro else segs.append(seg)

        ops = dict(base_ops or {})
        if remix.get("random_mirror") and random.random() < 0.55:
            ops["mirror"] = True
        if remix.get("random_crop"):
            ops["crop"] = {k: random.choice([0, 0, 0, 2, 3, 4, 5])
                           for k in ("top", "bottom", "left", "right")}
        if remix.get("random_speed"):
            ops["speed"] = round(random.uniform(0.95, 1.18), 2)
        if remix.get("random_color"):
            b, c, s = random.choice(COLOR_PRESETS)
            ops.update(brightness=b, contrast=c, saturation=s)
        if titles:
            ops["title"] = random.choice(titles)
        if remix.get("random_title_style"):
            ops["title_style"] = random.choice(TITLE_STYLES)
        if bgm_paths:
            ops["bgm"] = {"path": random.choice(bgm_paths),
                          "volume": max(0.0, min(2.0, float(remix.get("bgm_volume") or 0.35)))}
        if remix.get("random_wm_pos") and ops.get("watermark"):
            ops["watermark"] = dict(ops["watermark"], position=random.choice(WM_POSITIONS))
        plans.append({"segs": segs, "ops": ops})
    return plans


# --------------------------------------------------------------------------------------
# 服务层
# --------------------------------------------------------------------------------------
class TaskService:
    def __init__(self, store: StateStore):
        self.store = store
        self.wake = threading.Event()
        self.procs = {}
        self.workers = 0
        self._wlock = threading.Lock()

    def spawn_worker(self):
        with self._wlock:
            self.workers += 1
        threading.Thread(target=self.worker_loop, daemon=True).start()

    def ensure_workers(self):
        """按设置里的并发数伸缩工作线程：改设置即时生效，无需重启。"""
        target = max(1, min(16, int(self.store.data["settings"].get("concurrency") or 2)))
        with self._wlock:
            need = target - self.workers
        for _ in range(max(0, need)):
            self.spawn_worker()
        if need < 0:
            self.wake.set()

    def tasks(self):
        with self.store.lock:
            return list(self.store.data["tasks"])

    def find(self, tid):
        with self.store.lock:
            for t in self.store.data["tasks"]:
                if t["id"] == tid:
                    return t
        return None

    def set_status(self, tid, **kw):
        with self.store.lock:
            t = self.find(tid)
            if not t:
                return None
            t.update(kw)
        self.store.save()
        return tid

    def _make_task(self, title, segs, ops, naming, user, project_id, project_name, kind):
        seq = self.store.next_seq()
        tid = uuid.uuid4().hex[:12]
        try:
            out_name = (naming.get("template") or "{产品}-{编号}-{序号}").format(
                产品=sanitize(naming.get("product") or "成片"),
                编号=sanitize(naming.get("code") or str(seq).zfill(3)),
                序号=str(seq).zfill(3),
                原名=sanitize(title),
                日期=time.strftime("%Y%m%d"),
                剪辑=sanitize(naming.get("editor") or ""),
            )
        except Exception:
            out_name = sanitize(title)
        # 画布基准取「时长最长的段」：混剪含片头时不会被短贴片带偏
        main_seg = max(segs, key=lambda s: s.get("dur") or 0) if segs else None
        src = probe(main_seg["path"]) if main_seg else {}
        return {
            "id": tid, "seq": seq, "kind": kind,
            "title": sanitize(title)[:40],
            "source_name": ("%d 段混剪" % len(segs)) if kind == "remix"
                           else os.path.basename(segs[0]["path"]),
            "source_path": segs[0]["path"] if segs else "",
            "segments": segs,
            "src_info": src,
            "ops": ops, "naming": naming or {},
            "user": user, "project_id": project_id, "project_name": project_name,
            "output_name": sanitize(out_name) + ".mp4",
            "output_rel": None, "status": "queued", "progress": 0, "stage": "排队中",
            "eta_sec": None, "error": None, "duration": None, "log": [],
            "created_at": now_ts(), "started_at": None, "finished_at": None,
        }

    def create(self, files, ops, naming, user="", project_id="", project_name="",
               remix=None, intro_path=None, outro_path=None):
        created = []
        if remix:
            plans = plan_remix(files, remix, ops, naming or {})
            product = (naming or {}).get("product") or "素材"
            for i, pl in enumerate(plans, 1):
                t = self._make_task("%s 混剪#%d" % (product, i), pl["segs"], pl["ops"],
                                    naming, user, project_id, project_name, "remix")
                with self.store.lock:
                    self.store.data["tasks"].append(t)
                created.append(t)
        else:
            for path in files:
                if not os.path.exists(path):
                    continue
                info = probe(path)
                dur = info["duration"]
                ts = max(0.0, float(ops.get("trim_start") or 0))
                te = max(0.0, float(ops.get("trim_end") or 0))
                if ts + te > dur - 0.5:
                    te = max(0.0, dur - ts - 0.5)
                segs = [{"path": path, "start": round(ts, 2),
                         "dur": round(max(0.5, dur - ts - te), 2),
                         "has_audio": info["has_audio"]}]
                for is_intro, ip in ((True, intro_path), (False, outro_path)):
                    if ip and os.path.isfile(ip):
                        ii = probe(ip)
                        seg = {"path": ip, "start": 0.0,
                               "dur": round(max(0.5, ii["duration"]), 2),
                               "has_audio": ii["has_audio"]}
                        segs.insert(0, seg) if is_intro else segs.append(seg)
                base = os.path.splitext(os.path.basename(path))[0]
                t = self._make_task(base, segs, ops, naming, user,
                                    project_id, project_name, "single")
                with self.store.lock:
                    self.store.data["tasks"].append(t)
                created.append(t)
        if created:
            log_op("AI 混剪" if remix else "创建任务",
                   "%d 条 · %s" % (len(created), created[0]["title"]), user)
        self.store.save()
        self.wake.set()
        return created

    def act(self, ids, action, user=""):
        labels = {"approve": "审核通过", "reject": "退回重剪",
                  "retry": "重新出片", "delete": "删除任务"}
        for tid in ids:
            t = self.find(tid)
            if not t:
                continue
            if action == "approve" and t["status"] == "review":
                self.set_status(tid, status="approved", stage="审核通过，可交付")
                log_op(labels[action], t["title"], user)
            elif action == "reject" and t["status"] in ("review", "approved", "failed"):
                self.set_status(tid, status="queued", progress=0, stage="退回重剪，排队中",
                                eta_sec=None, error=None)
                log_op(labels[action], t["title"], user)
                self.wake.set()
            elif action == "retry" and t["status"] in ("failed", "review", "approved"):
                self.set_status(tid, status="queued", progress=0, stage="排队中",
                                eta_sec=None, error=None)
                log_op(labels[action], t["title"], user)
                self.wake.set()
            elif action == "delete":
                p = self.procs.get(tid)
                if p:
                    try:
                        p.kill()
                    except Exception:
                        pass
                with self.store.lock:
                    self.store.data["tasks"] = [x for x in self.store.data["tasks"]
                                                if x["id"] != tid]
                if t.get("output_rel"):
                    try:
                        os.remove(os.path.join(DATA_DIR, t["output_rel"]))
                    except Exception:
                        pass
                self.store.save()
                log_op(labels[action], t["title"], user)
        return True

    def worker_loop(self):
        while True:
            limit = max(1, min(16, int(self.store.data["settings"].get("concurrency") or 2)))
            with self._wlock:
                if self.workers > limit:      # 并发被调小：空闲线程自行退出
                    self.workers -= 1
                    return
            task = None
            with self.store.lock:
                busy = len([t for t in self.store.data["tasks"] if t["status"] == "processing"])
                if busy < limit:
                    for t in self.store.data["tasks"]:
                        if t["status"] == "queued":
                            t["status"] = "processing"
                            t["started_at"] = now_ts()
                            t["stage"] = "生成剪辑指令"
                            task = {"id": t["id"]}
                            break
            if task:
                self.store.save()
                self.run_task(task["id"])
                continue
            self.wake.wait(timeout=1.0)
            self.wake.clear()

    def run_task(self, tid):
        task = self.find(tid)
        if not task:
            return
        started = now_ts()
        try:
            segs = task.get("segments") or []
            if not segs or not all(os.path.isfile(s["path"]) for s in segs):
                raise RuntimeError("片段文件缺失，素材可能被移动或删除")
            out_path = os.path.join(OUT_DIR, "%s_%s" % (tid, task["output_name"]))
            args, out_len = build_segments_command(task, out_path)
            with self.store.lock:
                t = self.find(tid)
                if t is not None:
                    t["log"] = (t.get("log") or [])[-20:] + [" ".join(args)]
            self.set_status(tid, stage="剪辑与导出", progress=1)
            p = run_hidden(args)
            self.procs[tid] = p
            buf, tail = b"", []
            while True:
                chunk = p.stdout.read(512)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "ignore").strip()
                    if s.startswith("out_time_us=") or s.startswith("out_time_ms="):
                        raw = s.split("=", 1)[1]
                        if raw.isdigit():
                            done = int(raw) / 1_000_000.0
                            pct = max(1, min(99, int(done / max(0.1, out_len) * 100)))
                            el = max(0.2, now_ts() - started)
                            eta = int(el * (out_len - done) / done) if done > 0.5 else None
                            self.set_status(tid, progress=pct, eta_sec=eta,
                                            stage="剪辑与导出 %d%%" % pct)
                    elif s and not s.startswith(("frame=", "fps=", "bitrate=", "total_size=",
                                                 "out_time=", "speed=", "progress=", "dup_frames",
                                                 "drop_frames", "stream_0")):
                        tail.append(s)
                        tail = tail[-12:]
            p.wait()
            self.procs.pop(tid, None)
            if p.returncode != 0 or not os.path.exists(out_path):
                raise RuntimeError("ffmpeg 失败：" + (" / ".join(tail[-4:]) or
                                                     "退出码 %s" % p.returncode))
            rel = os.path.relpath(out_path, DATA_DIR).replace("\\", "/")
            try:
                poster_for(out_path)          # 出片即预生成封面，前端零等待
            except Exception:
                pass
            self.set_status(tid, status="review", progress=100, stage="待人工审核",
                            eta_sec=0, output_rel=rel, finished_at=now_ts(),
                            duration=round(out_len, 1))
        except Exception as e:
            self.set_status(tid, status="failed", stage="处理失败",
                            error=(str(e) or "未知错误"), finished_at=now_ts())
            with self.store.lock:
                t = self.find(tid)
                if t is not None:
                    t["log"] = (t.get("log") or [])[-10:] + [traceback.format_exc()[-1500:]]
            self.store.save()


SERVICE = TaskService(STORE)


def boot_workers(_n=None):
    SERVICE.ensure_workers()


def monitor_workers():
    """每 2 秒对齐一次线程数，让「设置」里的并发改动即时生效。"""
    while True:
        try:
            SERVICE.ensure_workers()
        except Exception:
            traceback.print_exc()
        time.sleep(2.0)


# --------------------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".mp4": "video/mp4", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
        ".webp": "image/webp", ".mov": "video/quicktime", ".mp3": "audio/mpeg"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BatchCut/3.0"

    def log_message(self, fmt, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, code=400):
        self.send_json({"ok": False, "error": msg}, code)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def serve_file(self, path, ctype=None, extra=None):
        if not os.path.isfile(path):
            return self.send_error_json("文件不存在", 404)
        size = os.path.getsize(path)
        ctype = ctype or MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            try:
                s, e = (rng[6:].split("-", 1) + [""])[:2]
                if s:
                    start = int(s)
                if e:
                    end = min(int(e), size - 1)
                partial = True
            except Exception:
                pass
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.serve_file(os.path.join(WEB_DIR, "index.html"))
            if p.startswith("/static/"):
                target = os.path.normpath(os.path.join(WEB_DIR, p[len("/static/"):]))
                if not target.startswith(WEB_DIR):
                    return self.send_error_json("非法路径", 403)
                return self.serve_file(target)
            if p == "/api/state":
                tasks = sorted(SERVICE.tasks(), key=lambda t: t["created_at"], reverse=True)
                with STORE.lock:
                    users = list(STORE.data["users"])
                    assets = list(STORE.data["assets"])
                    projects = list(STORE.data["projects"])
                    oplog = list(reversed(STORE.data["oplog"][-150:]))
                return self.send_json({"ok": True, "tasks": tasks,
                                       "settings": STORE.data["settings"],
                                       "users": users, "assets": assets,
                                       "projects": projects, "oplog": oplog,
                                       "server_time": now_ts()})
            if p == "/api/oplog":
                with STORE.lock:
                    return self.send_json({"ok": True,
                                           "oplog": list(reversed(STORE.data["oplog"]))})
            if p == "/api/media":
                fp = unquote((q.get("path") or [""])[0])
                if not fp or not os.path.isfile(fp):
                    return self.send_error_json("文件不存在", 404)
                return self.serve_file(fp)
            if p == "/api/output":
                fp = os.path.normpath(os.path.join(DATA_DIR, unquote((q.get("rel") or [""])[0])))
                if not fp.startswith(DATA_DIR) or not os.path.isfile(fp):
                    return self.send_error_json("文件不存在", 404)
                return self.serve_file(fp)
            if p == "/api/zip":
                # 批量打包：只收有成片的任务，重名自动加序号
                ids = [x for x in (q.get("ids") or [""])[0].split(",") if x]
                if not ids:
                    return self.send_error_json("未选择任务")
                picks, used = [], set()
                for tid in ids:
                    t = SERVICE.find(tid)
                    if not t or not t.get("output_rel"):
                        continue
                    fp = os.path.normpath(os.path.join(DATA_DIR, t["output_rel"]))
                    if not (fp.startswith(DATA_DIR) and os.path.isfile(fp)):
                        continue
                    stem, ext = os.path.splitext(t.get("output_name") or os.path.basename(fp))
                    if t.get("project_name"):
                        stem = "%s_%s" % (sanitize(t["project_name"]), stem)
                    cand, k = stem + ext, 1
                    while cand in used:
                        k += 1
                        cand = "%s(%d)%s" % (stem, k, ext)
                    used.add(cand)
                    picks.append((fp, cand))
                if not picks:
                    return self.send_error_json("所选任务还没有成片")
                zip_path = os.path.join(OUT_DIR, ".pack_%s.zip" % uuid.uuid4().hex[:8])
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
                    for fp, arc in picks:
                        z.write(fp, arc)
                log_op("批量打包下载", "%d 条成片" % len(picks), (q.get("user") or [""])[0])
                STORE.save()
                zname = "切影工坊_成片_%s.zip" % time.strftime("%Y%m%d_%H%M%S")
                try:
                    return self.serve_file(zip_path, "application/zip", extra={
                        "Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(zname)})
                finally:
                    if os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                        except Exception:
                            pass
            if p == "/api/poster":
                # 封面：成片传 rel（相对 data 目录），素材传 path（绝对路径）
                rel = unquote((q.get("rel") or [""])[0])
                raw = unquote((q.get("path") or [""])[0])
                if rel:
                    fp = os.path.normpath(os.path.join(DATA_DIR, rel))
                    if not fp.startswith(DATA_DIR):
                        return self.send_error_json("非法路径", 403)
                else:
                    fp = raw
                if not fp or not os.path.isfile(fp):
                    return self.send_error_json("视频不存在", 404)
                jpg = poster_for(fp)
                if not jpg:
                    return self.send_error_json("封面生成中", 404)
                return self.serve_file(jpg, "image/jpeg",
                                       extra={"Cache-Control": "public, max-age=86400"})
            if p == "/api/folder":
                folder = unquote((q.get("path") or [""])[0]).strip('"').strip("'")
                if not folder or not os.path.isdir(folder):
                    return self.send_error_json("文件夹不存在或不可读")
                files = []
                for name in sorted(os.listdir(folder)):
                    fp = os.path.join(folder, name)
                    if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in VIDEO_EXT:
                        files.append({"name": name, "path": fp, "size": os.path.getsize(fp)})
                return self.send_json({"ok": True, "files": files})
            return self.send_error_json("接口不存在", 404)
        except Exception:
            traceback.print_exc()
            return self.send_error_json("服务器内部错误", 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p == "/api/tasks":
                body = self.read_json()
                files = [f for f in (body.get("files") or []) if isinstance(f, str)]
                if not files:
                    return self.send_error_json("请先选择素材")
                created = SERVICE.create(
                    files, body.get("ops") or {}, body.get("naming") or {},
                    user=body.get("user") or "",
                    project_id=body.get("project_id") or "",
                    project_name=body.get("project_name") or "",
                    remix=body.get("remix"),
                    intro_path=body.get("intro_path"),
                    outro_path=body.get("outro_path"))
                return self.send_json({"ok": True, "created": created})
            if p == "/api/upload":
                name = sanitize((q.get("name") or ["file"])[0], "file")
                offset = int((q.get("offset") or ["0"])[0])
                n = int(self.headers.get("Content-Length") or 0)
                fp = os.path.join(UP_DIR, name)
                mode = "r+b" if offset > 0 and os.path.exists(fp) else "wb"
                with open(fp, mode) as f:
                    if offset > 0:
                        f.seek(offset)
                    else:
                        f.seek(0)
                        f.truncate()
                    remain = n
                    while remain > 0:
                        chunk = self.rfile.read(min(1 << 18, remain))
                        if not chunk:
                            break
                        f.write(chunk)
                        remain -= len(chunk)
                return self.send_json({"ok": True, "name": name, "path": fp,
                                       "size": os.path.getsize(fp)})
            if p == "/api/action":
                body = self.read_json()
                SERVICE.act(body.get("ids") or [], body.get("action") or "",
                            body.get("user") or "")
                return self.send_json({"ok": True})
            if p == "/api/users":
                body = self.read_json()
                actor = body.get("actor") or ""
                if body.get("action") == "delete":
                    with STORE.lock:
                        STORE.data["users"] = [u for u in STORE.data["users"]
                                               if u["id"] != body.get("id")]
                    log_op("删除用户", body.get("name") or body.get("id", ""), actor)
                else:
                    name = sanitize(body.get("name") or "", "")
                    if not name:
                        return self.send_error_json("请填写姓名")
                    role = body.get("role") or "剪辑师"
                    with STORE.lock:
                        STORE.data["users"].append({"id": uuid.uuid4().hex[:8], "name": name,
                                                    "role": role, "note": body.get("note") or "",
                                                    "created_at": now_ts()})
                    log_op("新增用户", "%s（%s）" % (name, role), actor)
                STORE.save()
                return self.send_json({"ok": True})
            if p == "/api/assets":
                body = self.read_json()
                actor = body.get("actor") or ""
                if body.get("action") == "delete":
                    with STORE.lock:
                        STORE.data["assets"] = [a for a in STORE.data["assets"]
                                                if a["id"] != body.get("id")]
                    log_op("移除素材", body.get("name") or body.get("id", ""), actor)
                elif body.get("action") == "update":
                    with STORE.lock:
                        for a in STORE.data["assets"]:
                            if a["id"] == body.get("id"):
                                if "name" in body:
                                    a["name"] = sanitize(body["name"], a["name"])
                                if "tags" in body:
                                    a["tags"] = str(body["tags"])[:120]
                                if "script" in body:
                                    a["script"] = str(body["script"])[:2000]
                    log_op("编辑素材信息", body.get("name") or body.get("id", ""), actor)
                else:
                    path = body.get("path") or ""
                    if not path or not os.path.isfile(path):
                        return self.send_error_json("文件不存在，请先上传")
                    kind = body.get("kind") if body.get("kind") in ASSET_KINDS else "other"
                    aname = sanitize(body.get("name") or os.path.basename(path), "素材")
                    with STORE.lock:
                        STORE.data["assets"].append({"id": uuid.uuid4().hex[:8], "kind": kind,
                                                     "name": aname, "path": path,
                                                     "tags": str(body.get("tags") or "")[:120],
                                                     "script": str(body.get("script") or "")[:2000],
                                                     "size": os.path.getsize(path),
                                                     "created_at": now_ts()})
                    log_op("素材入库", "%s（%s）" % (aname, ASSET_KIND_LABEL[kind]), actor)
                STORE.save()
                return self.send_json({"ok": True})
            if p == "/api/projects":
                body = self.read_json()
                actor = body.get("actor") or ""
                if body.get("action") == "delete":
                    with STORE.lock:
                        STORE.data["projects"] = [pr for pr in STORE.data["projects"]
                                                  if pr["id"] != body.get("id")]
                    log_op("删除项目", body.get("name") or "", actor)
                else:
                    name = sanitize(body.get("name") or "", "")
                    if not name:
                        return self.send_error_json("请填写项目名称")
                    pr = {"id": uuid.uuid4().hex[:8], "name": name,
                          "product": sanitize(body.get("product") or name, name),
                          "code": sanitize(body.get("code") or "", ""),
                          "template": body.get("template") or "{产品}-{编号}-{序号}",
                          "note": body.get("note") or "", "created_at": now_ts()}
                    with STORE.lock:
                        STORE.data["projects"].append(pr)
                    log_op("新建项目", name, actor)
                STORE.save()
                return self.send_json({"ok": True})
            if p == "/api/settings":
                body = self.read_json()
                with STORE.lock:
                    for k in ("ffmpeg_path", "font_path", "concurrency", "crf", "preset"):
                        if k in body:
                            STORE.data["settings"][k] = body[k]
                STORE.save()
                log_op("修改设置", ", ".join("%s=%s" % (k, body[k]) for k in
                       ("ffmpeg_path", "font_path", "concurrency", "crf", "preset") if k in body),
                       body.get("actor") or "")
                SERVICE.wake.set()
                return self.send_json({"ok": True, "settings": STORE.data["settings"]})
            if p == "/api/ffmpeg/check":
                fp = ffmpeg_bin()
                ok = os.path.isfile(fp) or shutil.which(fp) is not None
                ver = ""
                if ok:
                    try:
                        pr = run_hidden([fp, "-version"])
                        out, _ = pr.communicate(timeout=20)
                        ver = (out or b"").decode("utf-8", "ignore").split("\n")[0]
                    except Exception:
                        pass
                return self.send_json({"ok": ok, "path": fp, "version": ver})
            return self.send_error_json("接口不存在", 404)
        except Exception:
            traceback.print_exc()
            return self.send_error_json("服务器内部错误", 500)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8971
    boot_workers()
    threading.Thread(target=monitor_workers, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("BatchCut Studio v3 已启动: http://127.0.0.1:%d" % port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
