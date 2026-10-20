"""命令行入口：list / bootstrap / fetch / build / clean。"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

from . import bootstrap
from . import deps_env  # noqa: F401  必须在 config 之前：预扫描 --deps/--profile/--config-dir
from .config import (ACTIVE_PROFILE, BUILD_ORDER, DEPS_FILE, ENV_OUTPUT, FFMPEG_DEP_FLAGS,
                     OPTIONAL_LIBRARIES, BuildContext, LIBRARIES, dep_group, dep_profile,
                     resolve_output_root, resolve_selection, whitelist)
from .recipes import REGISTRY
from .toolchains import TOOLCHAINS, create_toolchain, normalize_platform
from .utils import (CommandRunner, Logger, build_marker_name, git_head_sha,
                    static_lib_names, write_version_file)

PLATFORM_HELP = ("目标平台: windows(本机 MSVC) / android(交叉编译) / ios(mac 编译) / "
                 "ohos(OpenHarmony，需 OHOS SDK，Linux/macOS 宿主)")
ABI_HELP = ("windows: x64/x86/arm64; android: arm64-v8a/armeabi-v7a/x86_64; "
            "ios: arm64/x86_64/fat; ohos: arm64-v8a/armeabi-v7a/x86_64")


# ------------------------------------------------------------------ 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="ffmpeg 及其依赖库编译脚本（开播链路；产物目录必须由 -o/--output 显式指定）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(f"示例（--output 必填，也可改设环境变量 {ENV_OUTPUT}）:\n"
                "  python build.py bootstrap --platform windows --install\n"
                "  python build.py build --platform windows -o D:/work/TGCapture3rd\n"
                "  python build.py build --platform android --abi arm64-v8a -o D:/work/TGCapture3rd\n"
                "  python build.py build --platform ios --libs ffmpeg -o D:/work/TGCapture3rd\n"
                "  OHOS_SDK=~/ohos-sdk python build.py build --platform ohos -o D:/work/TGCapture3rd\n"
                "指定依赖锁定文件（默认 --profile capture → config/capture_deps.json）:\n"
                "  python build.py build --platform windows --config-dir D:/work/mycfg\n"
                "  python build.py build --platform windows --profile player   # config/player_deps.json\n"
                "  python build.py build --platform windows --deps D:/work/capture_deps.json\n"))
    parser.add_argument("-q", "--quiet", action="store_true", help="减少输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出平台/ABI/依赖库")
    _deps_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_boot = sub.add_parser("bootstrap", help="检查并补齐编译工具链")
    p_boot.add_argument("-p", "--platform", required=True, help=PLATFORM_HELP)
    p_boot.add_argument("--install", action="store_true", help="自动下载/安装缺失工具")
    _deps_args(p_boot)
    p_boot.set_defaults(func=cmd_bootstrap)

    p_fetch = sub.add_parser("fetch", help="只拉取源码（不落盘产物，无需 --output）")
    _common(p_fetch, require_output=False)
    p_fetch.set_defaults(func=cmd_fetch)

    p_build = sub.add_parser("build", help="编译依赖库（必须指定 --output）")
    _common(p_build, require_output=True)
    p_build.add_argument("--clean", action="store_true", help="编译前清理中间目录与旧产物")
    p_build.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    p_build.add_argument("--no-check", action="store_true", help="跳过工具链自检")
    p_build.add_argument("--ffmpeg-ref", default=None, help="ffmpeg 仓库分支/标签，默认用 config.py 中的 ref")
    p_build.add_argument("--lto", action="store_true", help="ffmpeg 开启 LTO（默认关闭）")
    p_build.add_argument("--enable-nvenc", action="store_true", help="Windows 启用 NVENC（需 nv-codec-headers）")
    p_build.set_defaults(func=cmd_build)

    p_clean = sub.add_parser("clean", help="清理中间产物（--all 时还需 --output）")
    _common(p_clean, require_output=True)
    p_clean.add_argument("--all", action="store_true", help="同时删除 TGCapture3rd 中对应库")
    p_clean.set_defaults(func=cmd_clean)
    return parser


def _common(parser: argparse.ArgumentParser, require_output: bool = True) -> None:
    parser.add_argument("-p", "--platform", required=True, help=PLATFORM_HELP)
    parser.add_argument("--abi", nargs="*", default=None, help=ABI_HELP)
    parser.add_argument("--libs", default="all", help="要处理的库，逗号分隔")
    parser.add_argument("-o", "--output", default=None,
                        help=f"产物输出目录（必填；也可设环境变量 {ENV_OUTPUT}）")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="并行任务数")
    parser.add_argument("--toolchain", default="auto", choices=["auto", "msvc", "mingw"], help="Windows 工具链偏好")
    parser.add_argument("--ndk", default=None, help="Android NDK 路径")
    parser.add_argument("--api", type=int, default=21, help="Android API，默认 21")
    parser.add_argument("--ohos-sdk", default=None, help="OHOS SDK 根目录（含 native/），默认取 $OHOS_SDK")
    parser.add_argument("--min-version", default="12.0", help="iOS 最低版本，默认 12.0")
    parser.add_argument("--force-fetch", action="store_true", help="强制重新拉取源码")
    _deps_args(parser)


def _deps_args(parser: argparse.ArgumentParser) -> None:
    """依赖锁定文件的来源（值由 deps_env 在 import 期解析，这里只为 --help 与校验）。"""
    parser.add_argument("--config-dir", default=None,
                        help="依赖锁定文件所在目录（默认 <工程>/config）")
    parser.add_argument("--profile", default=None,
                        help=f"用途：config/<profile>_deps.json（默认 {deps_env.DEFAULT_PROFILE}，开播；播放用 player）")
    parser.add_argument("--deps", default=None, help="直接指定依赖锁定文件路径（最高优先级）")


PROFILE_MARKER = ".build_profile"        # 产物根目录里记录该目录属于哪个 profile


def _profile_summary() -> str:
    wl = whitelist(ACTIVE_PROFILE)
    return (f"裁剪白名单: 编码器={','.join(wl['encoders']) or '-'} "
            f"解码器={','.join(wl['decoders'])} 封装={','.join(wl['muxers']) or '-'} "
            f"解封装={','.join(wl['demuxers'])} 协议={','.join(wl['protocols'])} / "
            f"ffmpeg 外部库开关: {' '.join(FFMPEG_DEP_FLAGS) or '无'}")


def _ensure_output_profile(ctx: BuildContext) -> None:
    """产物目录 <lib>/<platform>/<abi> 不区分 profile，混放会互相覆盖，这里挡一道。"""
    marker = ctx.output_root / PROFILE_MARKER
    if marker.exists():
        existing = marker.read_text(encoding="utf-8").strip()
        if existing and existing != ctx.profile:
            raise RuntimeError(
                f"输出目录已用于 profile={existing} 的产物：{ctx.output_root}\n"
                f"  当前 profile={ctx.profile}，产物路径相同（<lib>/<platform>/<abi>），会覆盖旧产物。\n"
                f"  请换一个 -o 目录（例如 {ctx.output_root}-{ctx.profile}）；"
                f"确认要混放则删除 {marker} 后重试")
        return
    ctx.output_root.mkdir(parents=True, exist_ok=True)
    marker.write_text(ctx.profile, encoding="utf-8")


# ------------------------------------------------------------------ 命令
def cmd_list(args, logger: Logger) -> int:
    logger.section(f"依赖锁定文件 profile={ACTIVE_PROFILE}")
    logger.info(str(DEPS_FILE))
    logger.info(_profile_summary())
    logger.section("平台")
    for name, cls in TOOLCHAINS.items():
        logger.info(f"{name:<8} ABI={','.join(cls.default_abis)}")
    logger.section("依赖库（开播链路必须）")
    for name in BUILD_ORDER:
        lib = LIBRARIES[name]
        logger.info(f"{name:<10} {lib.version:<10} {lib.summary}")
    logger.section("可选（默认不构建，按需 --libs json,spdlog）")
    for name in OPTIONAL_LIBRARIES:
        lib = LIBRARIES[name]
        logger.info(f"{name:<10} {lib.version:<10} {lib.summary}")
    logger.info("不使用 x264 / x265 / v265")
    return 0


def cmd_bootstrap(args, logger: Logger) -> int:
    ctx = make_context(args, logger, require_output=False)
    result = bootstrap.check(ctx.platform, logger, ctx)
    bootstrap.report(result, logger)
    if args.install and result.missing:
        for action in bootstrap.install(ctx.platform, result, logger, CommandRunner(logger)):
            logger.info(action)
        bootstrap.report(bootstrap.check(ctx.platform, logger, ctx), logger)
    return 0 if not result.missing else 1


def cmd_fetch(args, logger: Logger) -> int:
    ctx = make_context(args, logger, require_output=False)
    for name in _ordered(args.libs):
        logger.section(f"fetch {name}")
        ctx.sources.ensure(LIBRARIES[name].source, ctx.source_dir(name), ctx.force_fetch)
    logger.info("源码已就绪")
    return 0


def cmd_build(args, logger: Logger) -> int:
    ctx = make_context(args, logger)
    _ensure_output_profile(ctx)
    if not args.no_check and not args.dry_run:
        bootstrap.ensure_ready(ctx.platform, logger, ctx.runner, ctx)
    ctx.toolchain.prepare()

    libraries = _ordered(args.libs)
    if args.clean:
        _clean(ctx, libraries, output_too=True)

    logger.section(f"开始编译: {ctx.summary}")
    logger.info(f"依赖锁定文件: {DEPS_FILE} (profile={ACTIVE_PROFILE})")
    logger.info(_profile_summary())
    results = {}
    for name in libraries:
        started = time.time()
        logger.section(f"build {name} {LIBRARIES[name].version}")
        ctx.sources.ensure(LIBRARIES[name].source, ctx.source_dir(name), ctx.force_fetch)
        stages = {}
        for abi in ctx.abis:
            logger.step(f"{name} [{abi}] 开始编译")
            stages[abi] = REGISTRY[name](ctx, abi)
            logger.info(f"{name} [{abi}] 完成 ({time.time() - started:.1f}s)")
        output = ctx.toolchain.finalize(name, stages)
        results[name] = output
        _write_manifest(ctx, name, output)

    logger.section("产物汇总")
    for name, output in results.items():
        logger.info(f"{name:<10} -> {output}")
        for item in static_lib_names(output / "lib"):
            logger.info(f"{'':<14}lib/{item}")
    logger.info(f"全部完成，共 {len(results)} 个库")
    return 0


def cmd_clean(args, logger: Logger) -> int:
    ctx = make_context(args, logger)
    _clean(ctx, _ordered(args.libs), output_too=args.all)
    logger.info("清理完成")
    return 0


# ------------------------------------------------------------------ 辅助
def _ordered(selection: str) -> List[str]:
    from .config import BUILD_ORDER
    names = resolve_selection(selection)
    ordered = [n for n in BUILD_ORDER if n in names]
    return ordered + [n for n in names if n not in ordered]


def make_context(args, logger: Logger, require_output: bool = True) -> BuildContext:
    platform = normalize_platform(args.platform)
    ctx = BuildContext(
        platform=platform,
        abis=[],
        output_root=resolve_output_root(getattr(args, "output", None), required=require_output),
        jobs=getattr(args, "jobs", 0) or 0,
        force_fetch=getattr(args, "force_fetch", False),
        dry_run=getattr(args, "dry_run", False),
        ffmpeg_ref=getattr(args, "ffmpeg_ref", None),
        ffmpeg_lto=getattr(args, "lto", False),
        enable_nvenc=getattr(args, "enable_nvenc", False),
    )
    ctx.logger = logger
    ctx.runner = CommandRunner(logger, dry_run=ctx.dry_run, log_dir=ctx.log_root)
    ctx.sources.runner = ctx.runner
    ctx.sources.logger = logger
    ctx.toolchain = create_toolchain(platform, ctx, toolchain=getattr(args, "toolchain", "auto"),
                                     ndk=getattr(args, "ndk", None), api=getattr(args, "api", 21),
                                     ohos_sdk=getattr(args, "ohos_sdk", None),
                                     min_version=getattr(args, "min_version", "12.0"))
    ctx.toolchain.logger = logger
    ctx.toolchain.runner = ctx.runner
    ctx.abis = ctx.toolchain.expand_abis(getattr(args, "abi", None))
    return ctx


def _clean(ctx, libraries: List[str], output_too: bool = False) -> None:
    for name in libraries:
        for abi in ctx.abis:
            work = ctx.work_dir(name, abi)
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
        if output_too:
            target = ctx.output_root / name / ctx.platform
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                ctx.logger.info(f"删除产物 {target}")


def _write_manifest(ctx, name: str, output: Path) -> None:
    import platform as pl

    lib = LIBRARIES[name]
    commit = git_head_sha(ctx.source_dir(name))
    abi = ctx.abis[0] if len(ctx.abis) == 1 else (",".join(ctx.abis) if ctx.abis else "")
    now = datetime.now()
    profile = dep_profile(name)
    marker = build_marker_name(name, lib.version, ctx.platform, abi,
                               getattr(ctx.toolchain, "compiler_id", "unknown"),
                               commit=commit, when=now, profile=profile)
    write_version_file(output / marker, [
        f"library    : {name}",
        f"version    : {lib.version}",
        f"commit     : {commit or '-'}",
        f"profile    : {profile or '-'}",
        f"group      : {dep_group(name) or '-'}",
        f"summary    : {lib.summary}",
        f"platform   : {ctx.platform} ({', '.join(ctx.abis)})",
        f"compiler   : {getattr(ctx.toolchain, 'compiler_id', 'unknown')}",
        f"build time : {now.isoformat(timespec='seconds')}",
        f"build host : {pl.system()} {pl.machine()}",
        f"source     : {ctx.source_dir(name)}",
    ])
    ctx.logger.info(f"[{name}] 版本标记 -> {marker}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = Logger(verbose=not args.quiet)
    try:
        return args.func(args, logger)
    except KeyboardInterrupt:
        logger.error("已取消")
        return 130
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1
