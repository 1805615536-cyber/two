#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""获取 ffmpeg 二进制到 tools/ 目录（首次运行自动调用，也可手动执行）。

用法：
    python tools/fetch_ffmpeg.py [--force]

获取顺序：
    1. tools/ 下已有 ffmpeg → 直接退出
    2. 系统 PATH 里已有 ffmpeg → 复制一份到 tools/
    3. 用 pip 从镜像源安装 imageio-ffmpeg，把其中的 ffmpeg 拷出来
    4. 直接下载指定版本的 imageio-ffmpeg wheel 并解包

只依赖 Python 标准库，网络走系统代理环境变量。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
TARGET = os.path.join(HERE, EXE)

# 国内可用的 PyPI 镜像（按顺序尝试）
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]

# 兜底直链：imageio-ffmpeg 0.5.1 win_amd64 wheel（内含 ffmpeg 静态版）
FALLBACK_WHEELS = [
    "https://pypi.tuna.tsinghua.edu.cn/packages/a9/1c/1b9c72bf839def47626436ea5ebaf643404f7850482c5fafd71a3deeaa94/imageio_ffmpeg-0.5.1-py3-none-win_amd64.whl",
]


def log(msg: str) -> None:
    print("[fetch-ffmpeg] " + msg, flush=True)


def copy_from_path() -> bool:
    p = shutil.which("ffmpeg")
    if not p:
        return False
    try:
        shutil.copy2(p, TARGET)
        log("已从系统 PATH 复制 ffmpeg：%s" % p)
        return True
    except Exception as e:
        log("复制失败：%s" % e)
        return False


def find_ffmpeg_in(root: str) -> str | None:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() == "ffmpeg.exe" or f == "ffmpeg":
                return os.path.join(dirpath, f)
    return None


def try_pip() -> bool:
    for mirror in MIRRORS:
        tmp = tempfile.mkdtemp(prefix="bc_ffmpeg_")
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--quiet",
               "--target", tmp, "-i", mirror, "imageio-ffmpeg"]
        log("尝试 pip 安装（%s）…" % mirror)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
        except Exception as e:
            log("pip 调用失败：%s" % e)
            continue
        if r.returncode == 0:
            src = find_ffmpeg_in(tmp)
            if src:
                shutil.copy2(src, TARGET)
                shutil.rmtree(tmp, ignore_errors=True)
                log("已从 imageio-ffmpeg 包中提取 ffmpeg")
                return True
        else:
            tail = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-1:]
            log("pip 失败：%s" % (tail[0] if tail else "unknown"))
        shutil.rmtree(tmp, ignore_errors=True)
    return False


def try_direct() -> bool:
    import urllib.request

    for url in FALLBACK_WHEELS:
        log("尝试直接下载：%s" % url.split("/")[-1])
        tmp = os.path.join(tempfile.mkdtemp(prefix="bc_wheel_"), "pkg.whl")
        try:
            with urllib.request.urlopen(url, timeout=180) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            with zipfile.ZipFile(tmp) as z:
                z.extractall(os.path.dirname(tmp))
            src = find_ffmpeg_in(os.path.dirname(tmp))
            if src:
                shutil.copy2(src, TARGET)
                log("已从 wheel 中提取 ffmpeg")
                return True
            log("wheel 内未找到 ffmpeg 可执行文件")
        except Exception as e:
            log("下载失败：%s" % e)
    return False


def verify() -> bool:
    if not os.path.isfile(TARGET):
        return False
    try:
        r = subprocess.run([TARGET, "-hide_banner", "-version"], capture_output=True, timeout=30)
        if r.returncode != 0:
            return False
        first = (r.stdout or b"").decode("utf-8", "ignore").splitlines()[:1]
        log("ffmpeg 可用：%s" % (first[0] if first else "ok"))
        return True
    except Exception:
        return False


def main() -> int:
    force = "--force" in sys.argv
    if os.path.isfile(TARGET) and not force:
        if verify():
            log("已存在，跳过。")
            return 0
        log("已存在但无法运行，重新获取…")

    if os.name != "nt":
        log("非 Windows 环境：请用系统包管理器安装 ffmpeg（如 apt install ffmpeg / brew install ffmpeg）。")
        return 1

    if copy_from_path() and verify():
        return 0
    if try_pip() and verify():
        return 0
    if try_direct() and verify():
        return 0

    log("自动获取失败。请手动下载 ffmpeg 静态包（https://www.gyan.dev/ffmpeg/builds/ 或 "
        "https://github.com/BtbN/FFmpeg-Builds/releases），把 ffmpeg.exe 放到：%s" % HERE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
