#!/usr/bin/env bash
# macOS 上编译 iOS 依赖（真机 + 模拟器 lipo 合并）
# 用法: ./scripts/build_ios.sh [fat|arm64|x86_64] [输出目录]
#   输出目录不给时用环境变量 FFMPEG_3RD_OUTPUT，再没有则默认 ../TGCapture3rd
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${2:-${FFMPEG_3RD_OUTPUT:-$(cd .. && pwd)/TGCapture3rd}}"

python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios --abi "${1:-fat}" -o "$OUT"
