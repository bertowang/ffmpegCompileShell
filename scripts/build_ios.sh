#!/usr/bin/env bash
# macOS 上编译 iOS 依赖（真机 + 模拟器 lipo 合并），产物 -> ../TGCapture3rd
# 用法: ./scripts/build_ios.sh [fat|arm64|x86_64]
set -euo pipefail
cd "$(dirname "$0")/.."

python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios --abi "${1:-fat}"
