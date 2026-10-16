#!/usr/bin/env bash
# macOS/Linux 上编译 Android 依赖，产物 -> ../TGCapture3rd
# 用法: ./scripts/build_android.sh [abi] [api]
set -euo pipefail
cd "$(dirname "$0")/.."

ABI="${1:-arm64-v8a}"
API="${2:-21}"

python3 build.py bootstrap --platform android --install
python3 build.py build --platform android --abi "$ABI" --api "$API"
