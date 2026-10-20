"""构建配置：依赖清单、ffmpeg 裁剪白名单、路径约定、构建上下文。

新增/升级一个库：只改这里的 ``LIBRARIES`` 与 ``BUILD_ORDER``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .deps_env import active_profile, deps_file  # 先于 DEPS_FILE 解析出锁定文件路径
from .utils import ArchiveSource, CommandRunner, GitSource, Logger, SourceManager, cpu_count

# ----------------------------------------------------------------- 路径约定
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR_NAME = "TGCapture3rd"          # 开播依赖库产物根目录
ENV_OUTPUT = "FFMPEG_3RD_OUTPUT"                  # 可用环境变量覆盖

SOURCE_DIR = "sources"
BUILD_DIR = "build"
CACHE_DIR = ".cache"
TOOLS_DIR = "tools"

# ----------------------------------------------------------------- 源码仓库
# 所有依赖的「地址 / 版本 / commit」统一锁定在 config/<profile>_deps.json：
# 只要确定该文件，任何时候都能编出完全相同的目标库（升级版本只改锁定文件）。
# 文件位置由 deps_env 解析：--deps > --profile/FFMPEG_3RD_PROFILE > --config-dir，
# 默认 config/capture_deps.json（开播）；播放将来用 config/player_deps.json。
DEPS_FILE = deps_file()
ACTIVE_PROFILE = active_profile()
FFMPEG_REPO = "git@git.woa.com:EST/media/e-ffmpeg/ffmpeg-6.1.6.git"   # 兼容旧引用
FFMPEG_REF = "master"


def _load_deps() -> dict:
    if not DEPS_FILE.exists():
        raise RuntimeError(
            f"缺少依赖锁定文件: {DEPS_FILE}\n"
            f"  当前 profile={ACTIVE_PROFILE}；用 --config-dir <目录> / --profile <名称> / --deps <文件> 指定，"
            f"或设置环境变量 FFMPEG_3RD_CONFIG_DIR / FFMPEG_3RD_PROFILE / FFMPEG_3RD_DEPS")
    with DEPS_FILE.open(encoding="utf-8") as fp:
        return json.load(fp)


DEPS = _load_deps()


def _iter_deps():
    """按 groups → [profiles] → libraries 展开，返回 (group, profile, name, spec)。"""
    for group_name, group in DEPS.get("groups", {}).items():
        profiles = group.get("profiles")
        if profiles:
            for profile_name, profile in profiles.items():
                if not profile.get("enabled", True):
                    continue
                for name, spec in profile.get("libraries", {}).items():
                    yield group_name, profile_name, name, spec
        else:
            for name, spec in group.get("libraries", {}).items():
                yield group_name, "", name, spec


# 扁平索引：库名 -> (group, profile, spec)
DEPS_INDEX: Dict[str, tuple] = {name: (g, p, spec) for g, p, name, spec in _iter_deps()}
DEPS_LIBRARIES: Dict[str, dict] = {name: spec for name, (_, _, spec) in DEPS_INDEX.items()}


def dep(name: str) -> dict:
    try:
        return DEPS_LIBRARIES[name]
    except KeyError:
        raise RuntimeError(f"{DEPS_FILE.name} 缺少库定义: {name}") from None


def dep_group(name: str) -> str:
    """所在分组：ffmpeg / ffmpeg_deps / standalone。"""
    return DEPS_INDEX[name][0] if name in DEPS_INDEX else ""


def dep_profile(name: str) -> str:
    """用途标记（capture/player），无 profile 的库返回空串。"""
    return DEPS_INDEX[name][1] if name in DEPS_INDEX else ""


def _source_for(spec: dict):
    """按锁定文件条目构造源码描述：git 优先用 commit 锁定，其次 ref/tag。"""
    if spec.get("kind") == "git":
        return GitSource(spec["url"],
                         ref=spec.get("ref") or spec.get("tag") or "master",
                         commit=spec.get("commit") or None)
    return ArchiveSource(spec["url"],
                         strip_components=int(spec.get("strip_components", 1)),
                         sha256=spec.get("sha256") or None)


def dep_source(name: str):
    return _source_for(dep(name))


# OHOS 官方 ffmpeg 没有 OpenHarmony 媒体框架适配，故该平台改用 openharmony-tpc 的 fork
# （--enable-ohosavcodecdecoder 只有这个 fork 才有），配置见锁定文件的 platform_overrides
FFMPEG_PLATFORM_SOURCE: Dict[str, object] = {
    platform: _source_for(spec)
    for platform, spec in ((DEPS_LIBRARIES.get("ffmpeg") or {}).get("platform_overrides") or {}).items()
}
# OHOS 平台追加的 ffmpeg 开关；改用官方 ffmpeg 源码时把这里置空
OHOS_FFMPEG_EXTRA_ARGS: List[str] = ["--enable-ohosavcodecdecoder"]

# ------------------------------------------------------- ffmpeg 裁剪白名单
# 裁剪白名单按 profile 分组：开播要编码+封装+推流，播放要解封装+解码+渲染，
# 两者组件集不同，因此不能共用一份。--profile 决定用哪一份。
CAPTURE_WHITELIST: Dict[str, object] = {
    "description": "开播 = 采集 → 编码 → 封装(flv/mpegts) → 推流(rtmp/rtmps/srt)，其余组件全部裁掉",
    # 开播 = 采集 → 编码 → 封装(flv/mpegts) → 推流(rtmp/rtmps/srt)
    "encoders": ["libopenh264", "libfdk_aac", "aac"],
    "platform_encoders": {"windows": [], "android": ["h264_mediacodec"], "ios": ["h264_videotoolbox"]},
    "decoders": ["h264", "aac", "png"],
    "muxers": ["flv", "mpegts"],
    "demuxers": ["mov", "flv", "mp4"],
    "parsers": ["h264", "aac"],
    "bsfs": ["h264_mp4toannexb", "aac_adtstoasc", "null"],
    "filters": ["null", "anull", "format", "aformat", "scale", "aresample", "overlay"],
    "protocols": ["file", "tcp", "tls", "rtmp", "rtmps", "srt"],
}

PLAYER_WHITELIST: Dict[str, object] = {
    "description": "播放 = 解协议/解封装 → 解码 → 渲染；不编任何编码器与封装器",
    # 播放器不编码，故 encoders 为空（不生成 --enable-encoder）
    "encoders": [],
    "platform_encoders": {},
    "decoders": ["h264", "hevc", "aac", "mp3", "png", "mjpeg"],
    "muxers": [],
    "demuxers": ["mov", "mp4", "flv", "mkv", "mpegts", "hls"],
    "parsers": ["h264", "hevc", "aac", "mpegaudio"],
    "bsfs": ["h264_mp4toannexb", "hevc_mp4toannexb", "aac_adtstoasc", "null"],
    "filters": ["null", "anull", "format", "aformat", "scale", "aresample", "rotate", "transpose"],
    # 无 openssl 时不能开 https/tls；需要 https 就把 openssl 加进 player_deps.json 的 ffmpeg_deps 组
    "protocols": ["file", "http", "tcp", "hls"],
}

PROFILE_WHITELIST: Dict[str, Dict[str, object]] = {
    "capture": CAPTURE_WHITELIST,
    "player": PLAYER_WHITELIST,
}
DEFAULT_WHITELIST_PROFILE = "capture"

# 兼容旧引用（开播语义）
BASE_ENCODERS = CAPTURE_WHITELIST["encoders"]
PLATFORM_ENCODERS: Dict[str, List[str]] = CAPTURE_WHITELIST["platform_encoders"]
DECODERS = CAPTURE_WHITELIST["decoders"]
MUXERS = CAPTURE_WHITELIST["muxers"]
DEMUXERS = CAPTURE_WHITELIST["demuxers"]
PARSERS = CAPTURE_WHITELIST["parsers"]
BITSTREAM_FILTERS = CAPTURE_WHITELIST["bsfs"]
FILTERS = CAPTURE_WHITELIST["filters"]
PROTOCOLS = CAPTURE_WHITELIST["protocols"]


def whitelist(profile: str = "") -> Dict[str, object]:
    """取指定 profile 的裁剪白名单；未知 profile 回落到开播。"""
    return PROFILE_WHITELIST.get(profile or ACTIVE_PROFILE, CAPTURE_WHITELIST)


def whitelist_encoders(platform: str, nvenc: bool = False, profile: str = "") -> List[str]:
    wl = whitelist(profile)
    encoders = list(wl["encoders"]) + list(wl["platform_encoders"].get(platform, []))
    if nvenc and platform == "windows":
        encoders.append("h264_nvenc")
    return encoders


@dataclass(frozen=True)
class Library:
    name: str
    version: str
    source: object
    summary: str


# 说明：version / 源码地址 / commit 全部来自锁定文件 config/<profile>_deps.json，这里只保留用途描述。
# 锁定文件里没列的库不会出现在这里（例如 player 只有 ffmpeg/json/spdlog），
# 换 profile 即换一组库，无需改代码。
_LIBRARY_SUMMARY: Dict[str, str] = {
    "openssl": "TLS/HTTPS/RTMPS（已按 RTMPS 场景裁剪）",
    "srt": "SRT 推流协议（ffmpeg --enable-libsrt）",
    "fdk-aac": "AAC 音频软编码",
    "openh264": "H.264 视频软编码（不使用 x264/x265/v265）",
    "libyuv": "YUV/RGB 格式转换与缩放",
    "json": "nlohmann/json 配置解析（header-only）",
    # MediaX 的 ESLogger 直接基于 spdlog 实现（android_sink / msvc_sink / OHOS hilog），
    # 其 find_package(spdlog 1.14.1 REQUIRED) 会限制这里能升到的版本
    "spdlog": "C++ 日志库（MediaX/ESLogger 的日志实现；TGCapture 自带 Logger，可不链）",
    "ffmpeg": "封装 + 协议（按 profile 的裁剪白名单）",
}

LIBRARIES: Dict[str, Library] = {
    name: Library(name, dep(name)["version"], dep_source(name), _LIBRARY_SUMMARY[name])
    for name in _LIBRARY_SUMMARY if name in DEPS_LIBRARIES
}

# 必须要的库（被依赖方在前，ffmpeg 最后）；只取当前锁定文件里存在的
_ALL_BUILD_ORDER = ("openssl", "srt", "fdk-aac", "openh264", "libyuv", "ffmpeg")
# 默认不构建：json 是 header-only（MediaX 侧通常已由 3rd submodule 提供），
# spdlog 只有 MediaX 的 ESLogger 需要（TGCapture 自带 Logger），均按需 --libs xxx
_ALL_OPTIONAL = ("json", "spdlog")

BUILD_ORDER = tuple(n for n in _ALL_BUILD_ORDER if n in LIBRARIES)
OPTIONAL_LIBRARIES = tuple(n for n in _ALL_OPTIONAL if n in LIBRARIES)
DEFAULT_SELECTION = BUILD_ORDER

# ffmpeg 的外部库依赖与对应 configure 开关，全部来自锁定文件的 ffmpeg_deps 组：
# 开播组里有 openssl/srt/fdk-aac/openh264 → --enable-openssl/--enable-libsrt/...；
# 播放组为空 → 不带任何 --enable-*，也就不需要先编这些库。
FFMPEG_DEPENDENCIES: tuple = tuple(n for n in _ALL_BUILD_ORDER if dep_group(n) == "ffmpeg_deps")
FFMPEG_DEP_FLAGS: List[str] = [spec["ffmpeg_flag"] for n in FFMPEG_DEPENDENCIES
                               for spec in [DEPS_LIBRARIES[n]] if spec.get("ffmpeg_flag")]


def resolve_selection(selection: Optional[str]) -> List[str]:
    if not selection or selection.strip().lower() in ("all", "*"):
        return list(DEFAULT_SELECTION)
    names = [item.strip() for item in selection.split(",") if item.strip()]
    unknown = [n for n in names if n not in LIBRARIES]
    if unknown:
        raise KeyError(f"未知依赖库: {', '.join(unknown)}，可选: {', '.join(LIBRARIES)}")
    return names


# --------------------------------------------------------------- 构建上下文
@dataclass
class BuildContext:
    """一次构建的全部共享状态。"""

    platform: str
    abis: List[str]
    output_root: Path
    jobs: int = field(default_factory=cpu_count)
    force_fetch: bool = False
    dry_run: bool = False
    ffmpeg_ref: str = FFMPEG_REF
    ffmpeg_lto: bool = False
    enable_nvenc: bool = False
    profile: str = ACTIVE_PROFILE     # 用途（capture/player）：决定白名单、外部库开关、中间目录

    toolchain: object = None          # 由 cli 装配
    logger: Logger = field(default_factory=Logger)
    runner: CommandRunner = field(default_factory=CommandRunner)
    sources: Optional[SourceManager] = None

    def __post_init__(self) -> None:
        self.output_root = Path(self.output_root)
        if self.jobs <= 0:
            self.jobs = cpu_count()
        self.runner.log_dir = self.log_root
        self.sources = SourceManager(self.runner, self.logger, self.cache_root)

    # 路径
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def source_root(self) -> Path:
        return PROJECT_ROOT / SOURCE_DIR

    @property
    def build_root(self) -> Path:
        return PROJECT_ROOT / BUILD_DIR

    @property
    def cache_root(self) -> Path:
        # 按 profile 分开：下载包按 URL 命名，两个 profile 可能锁不同版本的同名包
        return PROJECT_ROOT / CACHE_DIR / self.profile

    @property
    def log_root(self) -> Path:
        # 日志也按 profile 分开，便于对照排错（同名日志不会被另一用途追加覆盖）
        return self.build_root / f"{self.profile}-logs"

    def source_dir(self, lib: str) -> Path:
        # 源码目录两个 profile 共用：同一 URL+commit 不必重复克隆；
        # 锁定值不同时由 .deps_pin 触发重新拉取，不会误用旧源码
        return self.source_root / lib

    def work_dir(self, lib: str, abi: str) -> Path:
        # 带 profile 前缀：capture 与 player 的 configure 开关不同，
        # 共用一个中间目录会互相污染（复用旧 config.h/目标文件）
        return self.build_root / f"{self.profile}-{self.platform}-{abi}" / lib

    def stage_dir(self, lib: str, abi: str) -> Path:
        return self.work_dir(lib, abi) / "install"

    def install_dir(self, lib: str, abi: Optional[str] = None) -> Path:
        return self.toolchain.install_dir(lib, abi)

    @property
    def summary(self) -> str:
        return f"平台={self.platform} ABI={','.join(self.abis)} 输出={self.output_root} 并行={self.jobs}"


def resolve_output_root(user_value: Optional[str] = None, required: bool = True) -> Path:
    """产物根目录：命令行 ``--output`` > 环境变量 ``FFMPEG_3RD_OUTPUT``。

    不再提供隐式默认值：落盘类命令（build/clean）必须显式给出目录，
    避免产物散落到"工程同级 TGCapture3rd"这种猜出来的位置。
    ``required=False`` 仅供不落盘的命令（list/bootstrap/fetch）使用。
    """
    candidate = user_value or os.environ.get(ENV_OUTPUT)
    if candidate:
        return Path(candidate).expanduser().resolve()
    if not required:
        return (PROJECT_ROOT.parent / DEFAULT_OUTPUT_DIR_NAME).resolve()
    raise RuntimeError(
        "未指定产物输出目录，必须显式给出，避免产物落到预期外的位置。\n"
        f"  方式一：-o/--output <目录>      例: python build.py build --platform windows -o D:/work/{DEFAULT_OUTPUT_DIR_NAME}\n"
        f"  方式二：export {ENV_OUTPUT}=<目录>（Windows: setx {ENV_OUTPUT} <目录>）"
    )
