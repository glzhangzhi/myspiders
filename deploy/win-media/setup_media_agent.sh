#!/usr/bin/env bash
# 3060ti 的 WSL 上一次性准备 media agent 的运行环境（幂等，可重跑）。
# 在 free-arm 上执行：  ssh wsl-3060ti 'bash -s' < deploy/win-media/setup_media_agent.sh
# agent.py 本身不用在这里装 —— 服务器每次运行前都会推一份最新的过去。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

HOME_DIR=/root/media-agent
mkdir -p "$HOME_DIR/tmp" /mnt/d/Downloads/YouTube "/mnt/d/Downloads/视频号"

# ffmpeg：合并高清音视频轨（没有它 yt-dlp 最高只能拿到 ~720p 的合流格式）
command -v ffmpeg >/dev/null || apt-get install -y -q ffmpeg python3-venv unzip

# yt-dlp 装在独立 venv：系统 python 是 3.14 + PEP 668，不能直接 pip install
[ -x "$HOME_DIR/.venv/bin/python" ] || python3 -m venv "$HOME_DIR/.venv"
"$HOME_DIR/.venv/bin/pip" install -q -U pip "yt-dlp[default]"

# deno：yt-dlp 2025.11 起解 YouTube 的 JS 签名需要外部 JS 运行时
if [ ! -x "$HOME_DIR/deno/bin/deno" ]; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$HOME_DIR/deno" sh -s -- -y --no-modify-path
fi

# ltaoo/wx_channels_download（视频号）：只放文件，**首次运行必须机主在 Windows 上手动双击**
# （它要装根证书、设系统代理，会弹 UAC）。校验和对不上就不放。
WX_DIR=/mnt/c/Users/Administrator/AppData/Local/wx_video_download
VER=v260907
if [ ! -f "$WX_DIR/wx_video_download.exe" ]; then
  mkdir -p "$WX_DIR"
  cd "$HOME_DIR/tmp"
  B=https://github.com/ltaoo/wx_channels_download/releases/download/$VER
  F=wx_video_download_safe_${VER}_windows_x86_64.zip
  curl -fsSL -o "$F" "$B/$F"
  curl -fsSL -o sums.txt "$B/wx_video_download_${VER}_checksums.txt"
  grep " $F\$" sums.txt | sha256sum -c -
  unzip -o -q "$F" -d "$WX_DIR"
  rm -f "$F" sums.txt
fi

echo "yt-dlp $("$HOME_DIR/.venv/bin/yt-dlp" --version) · $(ffmpeg -version | head -1 | cut -d' ' -f1-3) · $("$HOME_DIR/deno/bin/deno" --version | head -1)"
