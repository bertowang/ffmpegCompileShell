#!/usr/bin/env python3
"""FfmpegCompileShell 统一入口。

    python build.py list
    python build.py bootstrap --platform windows --install
    python build.py build --platform windows
    python build.py build --platform android --abi arm64-v8a
    python build.py build --platform ios
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ffmpeg_build.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
