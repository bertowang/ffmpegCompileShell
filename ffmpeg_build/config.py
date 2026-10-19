"""构建配置：依赖清单、ffmpeg 裁剪白名单、路径约定、构建上下文。

新增/升级一个库：只改这里的 ``LIBRARIES`` 与 ``BUILD_ORDER``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
FFMPEG_REPO = "git@git.woa.com:EST/media/e-ffmpeg/ffmpeg-6.1.6.git"
FFMPEG_REF = "master"

# OHOS 官方 ffmpeg 没有 OpenHarmony 媒体框架适配，故该平台改用 openharmony-tpc 的 fork
# （--enable-ohosavcodecdecoder 只有这个 fork 才有）
FFMPEG_PLATFORM_SOURCE: Dict[str, object] = {
    "ohos": GitSource("https://gitee.com/openharmony-tpc-incubate/FFmpeg.git", ref="ohos-n6.1.2_dev"),
}
# OHOS 平台追加的 ffmpeg 开关；改用官方 ffmpeg 源码时把这里置空
OHOS_FFMPEG_EXTRA_ARGS: List[str] = ["--enable-ohosavcodecdecoder"]

# ------------------------------------------------------- ffmpeg 裁剪白名单
# 开播 = 采集 → 编码 → 封装(flv/mpegts) → 推流(rtmp/rtmps/srt)，其余组件全部裁掉
BASE_ENCODERS = ["libopenh264", "libfdk_aac", "aac"]
PLATFORM_ENCODERS: Dict[str, List[str]] = {
    "windows": [],
    "android": ["h264_mediacodec"],
    "ios": ["h264_videotoolbox"],
}
DECODERS = ["h264", "aac", "png"]
MUXERS = ["flv", "mpegts"]
DEMUXERS = ["mov", "flv", "mp4"]
PARSERS = ["h264", "aac"]
BITSTREAM_FILTERS = ["h264_mp4toannexb", "aac_adtstoasc", "null"]
FILTERS = ["null", "anull", "format", "aformat", "scale", "aresample", "overlay"]
PROTOCOLS = ["file", "tcp", "tls", "rtmp", "rtmps", "srt"]


@dataclass(frozen=True)
class Library:
    name: str
    version: str
    source: object
    summary: str


LIBRARIES: Dict[str, Library] = {
    "openssl": Library(
        "openssl", "3.2.4",
        ArchiveSource("https://github.com/openssl/openssl/releases/download/openssl-3.2.4/openssl-3.2.4.tar.gz"),
        "TLS/HTTPS/RTMPS（已按 RTMPS 场景裁剪）",
    ),
    "srt": Library(
        "srt", "v1.5.4",
        ArchiveSource("https://github.com/Haivision/srt/archive/refs/tags/v1.5.4.tar.gz"),
        "SRT 推流协议（ffmpeg --enable-libsrt）",
    ),
    "fdk-aac": Library(
        "fdk-aac", "2.0.3",
        ArchiveSource("https://github.com/mstorsjo/fdk-aac/archive/refs/tags/v2.0.3.tar.gz"),
        "AAC 音频软编码",
    ),
    "openh264": Library(
        "openh264", "2.3.1",
        ArchiveSource("https://github.com/cisco/openh264/archive/refs/tags/v2.3.1.tar.gz"),
        "H.264 视频软编码（不使用 x264/x265/v265）",
    ),
    "libyuv": Library(
        "libyuv", "0b9e5c92",
        GitSource("https://chromium.googlesource.com/libyuv/libyuv",
                  commit="0b9e5c922deec7c9921ae385f6242a9a27754d6e"),
        "YUV/RGB 格式转换与缩放",
    ),
    "json": Library(
        "json", "3.11.3",
        ArchiveSource("https://github.com/nlohmann/json/releases/download/v3.11.3/include.zip",
                      strip_components=0),
        "nlohmann/json 配置解析（header-only）",
    ),
    # MediaX 的 ESLogger 直接基于 spdlog 实现（android_sink / msvc_sink / OHOS hilog），
    # 版本必须与其 find_package(spdlog 1.14.1 REQUIRED) 一致
    "spdlog": Library(
        "spdlog", "v1.14.1",
        GitSource("https://github.com/gabime/spdlog.git", ref="v1.14.1"),
        "C++ 日志库（MediaX/ESLogger 的日志实现；TGCapture 自带 Logger，可不链）",
    ),
    "ffmpeg": Library(
        "ffmpeg", "6.1.6",
        GitSource(FFMPEG_REPO, ref=FFMPEG_REF),
        "封装 + 协议（开播链路白名单裁剪）",
    ),
}

# 开播工程必须要的库（被依赖方在前，ffmpeg 最后）
BUILD_ORDER = ("openssl", "srt", "fdk-aac", "openh264", "libyuv", "ffmpeg")
# 默认不构建：json 是 header-only（MediaX 侧通常已由 3rd submodule 提供），
# spdlog 只有 MediaX 的 ESLogger 需要（TGCapture 自带 Logger），均按需 --libs xxx
OPTIONAL_LIBRARIES = ("json", "spdlog")
DEFAULT_SELECTION = BUILD_ORDER


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

    toolchain: object = None          # 由 cli 装配
    logger: Logger = field(default_factory=Logger)
    runner: CommandRunner = field(default_factory=CommandRunner)
    sources: Optional[SourceManager] = None

    def __post_init__(self) -> None:
        self.output_root = Path(self.output_root)
        if self.jobs <= 0:
            self.jobs = cpu_count()
        self.runner.log_dir = self.build_root / "logs"
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
        return PROJECT_ROOT / CACHE_DIR

    def source_dir(self, lib: str) -> Path:
        return self.source_root / lib

    def work_dir(self, lib: str, abi: str) -> Path:
        return self.build_root / f"{self.platform}-{abi}" / lib

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
