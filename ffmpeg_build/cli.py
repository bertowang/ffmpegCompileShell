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
from .config import (BUILD_ORDER, OPTIONAL_LIBRARIES, BuildContext, LIBRARIES,
                     resolve_output_root, resolve_selection)
from .recipes import REGISTRY
from .toolchains import TOOLCHAINS, create_toolchain, normalize_platform
from .utils import CommandRunner, Logger, static_lib_names, write_version_file

PLATFORM_HELP = "目标平台: windows(本机 MSVC) / android(mac 交叉编译) / ios(mac 编译)"


# ------------------------------------------------------------------ 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="ffmpeg 及其依赖库编译脚本（开播链路，产物输出到 TGCapture3rd）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例:\n"
                "  python build.py bootstrap --platform windows --install\n"
                "  python build.py build --platform windows\n"
                "  python build.py build --platform android --abi arm64-v8a\n"
                "  python build.py build --platform ios --libs ffmpeg\n"))
    parser.add_argument("-q", "--quiet", action="store_true", help="减少输出")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出平台/ABI/依赖库").set_defaults(func=cmd_list)

    p_boot = sub.add_parser("bootstrap", help="检查并补齐编译工具链")
    p_boot.add_argument("-p", "--platform", required=True, help=PLATFORM_HELP)
    p_boot.add_argument("--install", action="store_true", help="自动下载/安装缺失工具")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_fetch = sub.add_parser("fetch", help="只拉取源码")
    _common(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_build = sub.add_parser("build", help="编译依赖库")
    _common(p_build)
    p_build.add_argument("--clean", action="store_true", help="编译前清理中间目录与旧产物")
    p_build.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    p_build.add_argument("--no-check", action="store_true", help="跳过工具链自检")
    p_build.add_argument("--ffmpeg-ref", default="master", help="ffmpeg 仓库分支/标签，默认 master")
    p_build.add_argument("--lto", action="store_true", help="ffmpeg 开启 LTO（默认关闭）")
    p_build.add_argument("--enable-nvenc", action="store_true", help="Windows 启用 NVENC（需 nv-codec-headers）")
    p_build.set_defaults(func=cmd_build)

    p_clean = sub.add_parser("clean", help="清理中间产物")
    _common(p_clean)
    p_clean.add_argument("--all", action="store_true", help="同时删除 TGCapture3rd 中对应库")
    p_clean.set_defaults(func=cmd_clean)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--platform", required=True, help=PLATFORM_HELP)
    parser.add_argument("--abi", nargs="*", default=None,
                        help="windows: x64; android: arm64-v8a/armeabi-v7a/x86_64; ios: arm64/x86_64/fat")
    parser.add_argument("--libs", default="all", help="要处理的库，逗号分隔")
    parser.add_argument("-o", "--output", default=None, help="产物根目录，默认工程同级 TGCapture3rd")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="并行任务数")
    parser.add_argument("--toolchain", default="auto", choices=["auto", "msvc", "mingw"], help="Windows 工具链偏好")
    parser.add_argument("--ndk", default=None, help="Android NDK 路径")
    parser.add_argument("--api", type=int, default=21, help="Android API，默认 21")
    parser.add_argument("--min-version", default="12.0", help="iOS 最低版本，默认 12.0")
    parser.add_argument("--force-fetch", action="store_true", help="强制重新拉取源码")


# ------------------------------------------------------------------ 命令
def cmd_list(args, logger: Logger) -> int:
    logger.section("平台")
    for name, cls in TOOLCHAINS.items():
        logger.info(f"{name:<8} ABI={','.join(cls.default_abis)}")
    logger.section("依赖库（开播链路必须）")
    for name in BUILD_ORDER:
        lib = LIBRARIES[name]
        logger.info(f"{name:<10} {lib.version:<10} {lib.summary}")
    logger.section("可选（header-only，按需 --libs json）")
    for name in OPTIONAL_LIBRARIES:
        lib = LIBRARIES[name]
        logger.info(f"{name:<10} {lib.version:<10} {lib.summary}")
    logger.info("不使用 x264 / x265 / v265")
    return 0


def cmd_bootstrap(args, logger: Logger) -> int:
    ctx = make_context(args, logger)
    result = bootstrap.check(ctx.platform, logger, ctx)
    bootstrap.report(result, logger)
    if args.install and result.missing:
        for action in bootstrap.install(ctx.platform, result, logger, CommandRunner(logger)):
            logger.info(action)
        bootstrap.report(bootstrap.check(ctx.platform, logger, ctx), logger)
    return 0 if not result.missing else 1


def cmd_fetch(args, logger: Logger) -> int:
    ctx = make_context(args, logger)
    for name in _ordered(args.libs):
        logger.section(f"fetch {name}")
        ctx.sources.ensure(LIBRARIES[name].source, ctx.source_dir(name), ctx.force_fetch)
    logger.info("源码已就绪")
    return 0


def cmd_build(args, logger: Logger) -> int:
    ctx = make_context(args, logger)
    if not args.no_check and not args.dry_run:
        bootstrap.ensure_ready(ctx.platform, logger, ctx.runner, ctx)
    ctx.toolchain.prepare()

    libraries = _ordered(args.libs)
    if args.clean:
        _clean(ctx, libraries, output_too=True)

    logger.section(f"开始编译: {ctx.summary}")
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


def make_context(args, logger: Logger) -> BuildContext:
    platform = normalize_platform(args.platform)
    ctx = BuildContext(
        platform=platform,
        abis=[],
        output_root=resolve_output_root(getattr(args, "output", None)),
        jobs=getattr(args, "jobs", 0) or 0,
        force_fetch=getattr(args, "force_fetch", False),
        dry_run=getattr(args, "dry_run", False),
        ffmpeg_ref=getattr(args, "ffmpeg_ref", "master"),
        ffmpeg_lto=getattr(args, "lto", False),
        enable_nvenc=getattr(args, "enable_nvenc", False),
    )
    ctx.logger = logger
    ctx.runner = CommandRunner(logger, dry_run=ctx.dry_run, log_dir=ctx.build_root / "logs")
    ctx.sources.runner = ctx.runner
    ctx.sources.logger = logger
    ctx.toolchain = create_toolchain(platform, ctx, toolchain=getattr(args, "toolchain", "auto"),
                                     ndk=getattr(args, "ndk", None), api=getattr(args, "api", 21),
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
    write_version_file(output / "version.txt", [
        f"library    : {name}",
        f"version    : {lib.version}",
        f"summary    : {lib.summary}",
        f"platform   : {ctx.platform} ({', '.join(ctx.abis)})",
        f"build time : {datetime.now().isoformat(timespec='seconds')}",
        f"build host : {pl.system()} {pl.machine()}",
        f"source     : {ctx.source_dir(name)}",
    ])


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
