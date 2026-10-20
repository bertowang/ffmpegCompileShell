"""决定本次构建加载哪一份依赖锁定文件。

锁定文件按「用途 profile」分文件放在独立的 ``config/`` 目录：

    config/capture_deps.json    开播（当前）
    config/player_deps.json     播放（未来新增，无需改代码）

构建时显式指定（优先级从高到低）：

    --deps <file>           直接给锁定文件路径
    --profile <name>        config/<name>_deps.json
    --config-dir <dir>      锁定文件所在目录（默认 <工程>/config）
    (默认)                  <config_dir>/capture_deps.json

等价环境变量（CI/无人值守时用）：FFMPEG_3RD_DEPS / FFMPEG_3RD_PROFILE / FFMPEG_3RD_CONFIG_DIR。

本模块必须在 ``config`` 之前导入：它在 import 时预扫描 sys.argv，
把上述参数写进环境变量，config.py 再据此解析出 DEPS_FILE。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG_DIR_NAME = "config"
DEFAULT_PROFILE = "capture"          # 开播；播放将来用 player
DEFAULT_CONFIG_DIR = PROJECT_ROOT / CONFIG_DIR_NAME

ENV_DEPS_FILE = "FFMPEG_3RD_DEPS"
ENV_PROFILE = "FFMPEG_3RD_PROFILE"
ENV_CONFIG_DIR = "FFMPEG_3RD_CONFIG_DIR"


def config_dir() -> Path:
    raw = os.environ.get(ENV_CONFIG_DIR)
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CONFIG_DIR.resolve()


def active_profile() -> str:
    return (os.environ.get(ENV_PROFILE) or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE


def deps_file() -> Path:
    """当前生效的锁定文件路径：<config_dir>/<profile>_deps.json。"""
    raw = os.environ.get(ENV_DEPS_FILE)
    if raw:
        return Path(raw).expanduser().resolve()
    return config_dir() / f"{active_profile()}_deps.json"


def apply_from_argv(argv=None) -> None:
    """预扫描命令行（不校验未知参数），把锁定文件相关选项写进环境变量。"""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--deps")
    probe.add_argument("--profile")
    probe.add_argument("--config-dir", dest="config_dir")
    ns, _ = probe.parse_known_args(argv)
    if ns.config_dir:
        os.environ[ENV_CONFIG_DIR] = ns.config_dir
    if ns.profile:
        os.environ[ENV_PROFILE] = ns.profile
    if ns.deps:
        os.environ[ENV_DEPS_FILE] = ns.deps


apply_from_argv()
