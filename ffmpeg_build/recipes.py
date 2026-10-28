"""各依赖库的编译配方。

每个配方都是 ``build_xxx(ctx, abi) -> stage 目录``，在 :data:`REGISTRY` 中注册；
新增一个库只需在这里加一个函数 + 在 config.py 登记。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from . import config as C
from .utils import copy_tree, ensure_dir, which, write_version_file

# 当前正在构建的库名（供 cmake_* 辅助函数取 install prefix 与日志名）
CURRENT_LIB = {"name": ""}


def _set_current(lib: str) -> None:
    CURRENT_LIB["name"] = lib


# ----------------------------------------------------------------- CMake 辅助
def cmake_bin(ctx) -> str:
    """configure / build / install 三个阶段必须用同一个 cmake。

    OHOS 只能用 SDK 自带的 cmake（系统 cmake 与 ohos.toolchain.cmake 规则不兼容），
    若 build/install 仍从 PATH 取 cmake，就会出现"用 A 配置、用 B 编译"的错配。
    """
    return getattr(ctx.toolchain, "cmake_bin", "") or "cmake"


def cmake_configure(ctx, src: Path, build_dir: Path, abi: str, extra: Optional[List[str]] = None) -> None:
    build_dir = Path(build_dir)
    ensure_dir(build_dir)
    cmd = [cmake_bin(ctx), "-S", str(src), "-B", str(build_dir)]
    cmd += ctx.toolchain.cmake_args(abi)
    cmd += [f"-DCMAKE_INSTALL_PREFIX={ctx.stage_dir(CURRENT_LIB['name'], abi)}",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"]   # CMake 4 需要，兼容老项目(如 srt)
    cmd += list(extra or [])
    ctx.runner.run(cmd, cwd=src, env=ctx.toolchain.build_env(abi), log_name=f"{CURRENT_LIB['name']}-{abi}-cmake")


def cmake_build(ctx, build_dir: Path, abi: str, target: Optional[str] = None, lib: str = "") -> None:
    cmd = [cmake_bin(ctx), "--build", str(build_dir)]
    if getattr(ctx.toolchain, "needs_config_type", False):
        cmd += ["--config", "Release"]
    if target:
        cmd += ["--target", target]
    cmd += ["--parallel", str(ctx.jobs)]
    ctx.runner.run(cmd, cwd=build_dir, env=ctx.toolchain.build_env(abi), log_name=f"{lib}-{abi}-build")


def collapse_lib_dirs(stage: Path) -> None:
    """把 ``lib64`` / multiarch 子目录收敛进 ``lib``。

    RHEL 系发行版上 CMake 的 ``GNUInstallDirs`` 与 openssl 的 ``Configure`` 默认装到
    ``lib64``（Debian 系还有 ``lib/<multiarch>``），而 ffmpeg 的 configure 固定按
    ``<prefix>/lib`` 找依赖 —— 产物里若只有 ``lib64``，ffmpeg 会因为找不到库而失败。
    统一收敛到 ``lib``，各平台的产物布局才一致。
    """
    stage = Path(stage)
    lib_dir = stage / "lib"
    extra: List[Path] = []
    if (stage / "lib64").is_dir():
        extra.append(stage / "lib64")
    if lib_dir.is_dir():
        extra += [p for p in lib_dir.iterdir() if p.is_dir() and p.name.endswith("-linux-gnu")]
    for source in extra:
        ensure_dir(lib_dir)
        for entry in list(source.iterdir()):
            target = lib_dir / entry.name
            if target.exists():          # lib 下已有同名（如 pkgconfig）就保留 lib 的
                continue
            shutil.move(str(entry), str(target))
        shutil.rmtree(source, ignore_errors=True)


def cmake_install(ctx, build_dir: Path, abi: str, lib: str = "") -> None:
    cmd = [cmake_bin(ctx), "--install", str(build_dir)]
    if getattr(ctx.toolchain, "needs_config_type", False):
        cmd += ["--config", "Release"]
    ctx.runner.run(cmd, cwd=build_dir, env=ctx.toolchain.build_env(abi), log_name=f"{lib}-{abi}-install")
    if lib:                             # RHEL 系会装到 lib64，收敛成统一的 lib
        collapse_lib_dirs(ctx.stage_dir(lib, abi))


# ----------------------------------------------------------- 源码内构建隔离
def _in_source_copy(ctx, lib: str, abi: str, src: Path) -> Path:
    """为「源码内构建」的库（openssl / openh264 等 Makefile 直接在源码树里产生 .o/.a）
    准备一份按 profile+platform+abi 隔离的源码副本，构建只在副本里进行。

    这类库的 Makefile 没有 out-of-tree 选项；若直接在共用的 ``ctx.source_dir(lib)``
    里构建，切换平台/ABI 时上一轮残留的中间产物（不同容器格式 ELF/Mach-O/COFF，
    或同容器不同架构）会被 make 当作"已是最新"直接复用，混进新产物的 .a 里，
    导致链接器报 "archive member is neither ET_REL nor LLVM bitcode" 或大量
    undefined symbol —— 事后扫描文件魔数删残留 / 过滤归档成员只是在补救症状。
    根治办法是让每个 (profile, platform, abi) 天然拥有独立源码树：
    ``ctx.work_dir(lib, abi)`` 本就按三者隔离（见 config.py），构建前把共享源码
    复制进去即可，make 产生的所有中间文件只会落在这份专属副本里，不会反向
    污染共享的 ``sources/<lib>``，也不可能被其它平台/ABI 复用。

    复制是幂等的：共享源码的 ``.deps_pin``（内容）没变就复用已有副本里的旧目标
    文件（增量编译），锁定文件升级或 ``--force-fetch`` 时才整树重拷。
    """
    dest = ctx.work_dir(lib, abi) / "src"
    pin_file = src / ".deps_pin"
    pin = pin_file.read_text(encoding="utf-8", errors="ignore") if pin_file.is_file() else ""
    stamp = dest / ".src_pin"
    if (not ctx.force_fetch and dest.is_dir() and stamp.is_file()
            and stamp.read_text(encoding="utf-8", errors="ignore") == pin):
        return dest
    remove_tree(dest)
    ensure_dir(dest.parent)
    shutil.copytree(src, dest, symlinks=True)
    if pin:
        stamp.write_text(pin, encoding="utf-8")
    return dest


# ------------------------------------------------------------------ openssl
# 只做 RTMPS/TLS：关闭 apps/文档/测试与用不到的协议特性
OPENSSL_OPTIONS = [
    "no-shared", "no-apps", "no-tests", "no-docs", "no-async", "no-dso",
    "no-ssl3", "no-weak-ssl-ciphers", "no-comp", "no-dtls", "no-psk", "no-srtp",
]
IOS_OPENSSL_TARGETS = {
    "arm64": "ios64-xcrun",
    "x86_64": "iossimulator-x86_64-xcrun",
    "arm64-simulator": "iossimulator-arm64-xcrun",
    "i386": "iossimulator-i386-xcrun",
}


def build_openssl(ctx, abi: str) -> Path:
    _set_current("openssl")
    stage = ensure_dir(ctx.stage_dir("openssl", abi))
    shared_src = ctx.sources.ensure(C.dep_source("openssl", ctx.platform), ctx.source_dir("openssl"), ctx.force_fetch)
    # openssl 是源码内构建（Configure + make 直接在源码树产生 .o/.a），按 abi 隔离一份
    # 副本再构建，避免多平台/ABI 共用同一份源码树时互相踩到对方的 Makefile 与目标文件
    src = _in_source_copy(ctx, "openssl", abi, shared_src)
    toolchain, platform = ctx.toolchain, ctx.platform

    if platform == "windows":
        from .toolchains import WINDOWS_ABI
        env = toolchain.build_env(abi)
        perl = _native_perl(toolchain)
        if not perl:
            raise RuntimeError("openssl 需要原生 Windows perl，执行: python build.py bootstrap --platform windows --install")
        nmake = which("nmake", env)
        if not nmake:
            raise RuntimeError("openssl 需要 MSVC 的 nmake")
        prefix = str(stage).replace("\\", "/")
        # target 按 ABI 选（x64=VC-WIN64A / x86=VC-WIN32 / arm64=VC-WIN64-ARM），
        # 不能写死 VC-WIN64A：否则 x86/arm64 的 vcvars 环境下要么链接失败、要么产出 x64 库
        options = list(OPENSSL_OPTIONS)
        if not WINDOWS_ABI[abi]["nasm"]:
            # ARM64 的汇编走 armasm64 + perlasm 的 win64 方言，各 openssl 版本支持程度不一；
            # 只做 RTMPS/TLS，用 C 实现足够，换来"装了 ARM64 组件就能编过"的确定性
            options.append("no-asm")
        ctx.runner.run([perl, "Configure", WINDOWS_ABI[abi]["openssl"], *options,
                        f"--prefix={prefix}", f"--openssldir={prefix}/ssl"],
                       cwd=src, env=env, log_name=f"openssl-{abi}-configure")
        ctx.runner.run([nmake], cwd=src, env=env, log_name=f"openssl-{abi}-build")
        ctx.runner.run([nmake, "install_sw"], cwd=src, env=env, log_name=f"openssl-{abi}-install")
    else:
        if platform == "android":
            from .toolchains import ANDROID_ABI
            target = ANDROID_ABI[abi]["openssl"]
            configure = (f"./Configure {target} -D__ANDROID_API__={toolchain.api} "
                         f"{' '.join(OPENSSL_OPTIONS)} --prefix={stage} --openssldir={stage}/ssl")
        elif platform == "ohos":
            # OHOS 是 musl + linux 内核，直接用 openssl 的 linux-* target
            from .toolchains import OHOS_ABI
            target = OHOS_ABI[abi]["openssl"]
            configure = (f"./Configure {target} -fPIC {' '.join(OPENSSL_OPTIONS)} "
                         f"--prefix={stage} --openssldir={stage}/ssl")
        elif platform == "linux":
            # 本机 Linux：直接用 openssl 自带的 linux-* target（与 OHOS 同族，只是 glibc）
            from .toolchains import LINUX_ABI
            target = LINUX_ABI[abi]["openssl"]
            configure = (f"./Configure {target} -fPIC {' '.join(OPENSSL_OPTIONS)} "
                         f"--prefix={stage} --openssldir={stage}/ssl")
        elif platform == "macos":
            from .toolchains import MACOS_ABI
            configure = (f"./Configure {MACOS_ABI[abi]['openssl']} -mmacosx-version-min={toolchain.min_version} "
                         f"{' '.join(OPENSSL_OPTIONS)} --prefix={stage} --openssldir={stage}/ssl")
        else:
            configure = (f"./Configure {IOS_OPENSSL_TARGETS.get(abi, 'ios64-xcrun')} "
                         f"{' '.join(OPENSSL_OPTIONS)} --prefix={stage} --openssldir={stage}/ssl")
        toolchain.sh(f"{configure} && make -j{ctx.jobs} && make install_sw", cwd=src, abi=abi, log_name=f"openssl-{abi}")
        collapse_lib_dirs(stage)        # RHEL 系 openssl 默认装到 lib64

    # Windows：产物名与 ffmpeg 预期不一致，补别名与 .pc
    if platform == "windows":
        lib_dir = stage / "lib"
        for source, alias in (("libssl.lib", "ssl.lib"), ("libcrypto.lib", "crypto.lib")):
            if (lib_dir / source).exists() and not (lib_dir / alias).exists():
                shutil.copy2(lib_dir / source, lib_dir / alias)
        pc_file = ensure_dir(lib_dir / "pkgconfig") / "openssl.pc"
        if not pc_file.exists():
            pc_file.write_text(
                "prefix=${pcfiledir}/../..\nexec_prefix=${prefix}\nlibdir=${exec_prefix}/lib\n"
                "includedir=${prefix}/include\n\nName: OpenSSL\n"
                "Description: Secure Sockets Layer and cryptography libraries\n"
                f"Version: {C.LIBRARIES['openssl'].version}\n"
                "Libs: -L${libdir} -lssl -lcrypto\n"
                "Libs.private: -lws2_32 -lgdi32 -lcrypt32 -ladvapi32 -luser32\n"
                "Cflags: -I${includedir}\n", encoding="utf-8")
    return stage


def _native_perl(toolchain) -> Optional[str]:
    """openssl 的 Windows 构建要求 $^O 为 MSWin32（MSYS2 的 cygwin perl 不可用）。"""
    from .bootstrap import detect_native_perl
    return detect_native_perl(getattr(toolchain, "msys2_bins", None))


# ---------------------------------------------------------------------- srt
def build_srt(ctx, abi: str) -> Path:
    _set_current("srt")
    stage = ctx.stage_dir("srt", abi)
    src = ctx.sources.ensure(C.dep_source("srt", ctx.platform), ctx.source_dir("srt"), ctx.force_fetch)
    build_dir = ctx.work_dir("srt", abi) / "cmake-build"
    cmake_configure(ctx, src, build_dir, abi, extra=[
        "-DENABLE_SHARED=OFF", "-DENABLE_STATIC=ON", "-DENABLE_APPS=OFF",
        "-DENABLE_ENCRYPTION=OFF", "-DENABLE_UNITTESTS=OFF",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        f"-DCMAKE_PREFIX_PATH={ctx.install_dir('openssl', abi)}",
    ])
    cmake_build(ctx, build_dir, abi, lib="srt")
    cmake_install(ctx, build_dir, abi, lib="srt")

    if ctx.platform == "windows":
        lib_dir = stage / "lib"
        for name in ("srt_static.lib", "libsrt.lib"):
            if (lib_dir / name).exists() and not (lib_dir / "srt.lib").exists():
                shutil.copy2(lib_dir / name, lib_dir / "srt.lib")
    # ffmpeg 按包名 srt 找，而 srt 装的是 haisrt.pc（各平台皆然，不只是 Windows）；
    # 缺这份别名时 configure 会报 "srt not found using pkg-config"
    pc_dir = stage / "lib" / "pkgconfig"
    if (pc_dir / "haisrt.pc").exists() and not (pc_dir / "srt.pc").exists():
        shutil.copy2(pc_dir / "haisrt.pc", pc_dir / "srt.pc")
    return stage


# ------------------------------------------------------------------ fdk-aac
def build_fdk_aac(ctx, abi: str) -> Path:
    _set_current("fdk-aac")
    src = ctx.sources.ensure(C.dep_source("fdk-aac", ctx.platform), ctx.source_dir("fdk-aac"), ctx.force_fetch)
    build_dir = ctx.work_dir("fdk-aac", abi) / "cmake-build"
    cmake_configure(ctx, src, build_dir, abi, extra=[
        "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_PROGRAMS=OFF",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
    ])
    cmake_build(ctx, build_dir, abi, lib="fdk-aac")
    cmake_install(ctx, build_dir, abi, lib="fdk-aac")
    return ctx.stage_dir("fdk-aac", abi)


# ----------------------------------------------------------------- openh264
def build_openh264(ctx, abi: str) -> Path:
    _set_current("openh264")
    stage = ensure_dir(ctx.stage_dir("openh264", abi))
    shared_src = ctx.sources.ensure(C.dep_source("openh264", ctx.platform), ctx.source_dir("openh264"), ctx.force_fetch)
    # openh264 是源码内构建（make 直接在源码树产生 .o/.a），按 abi 隔离一份副本再构建：
    # 避免多平台/ABI 共用同一份源码树时互相踩到对方残留的目标文件（例如先编 iOS 再编
    # Android，Android 的 .a 会把源码树里残留的 iOS Mach-O .o 当作"已是最新"直接混进去）
    src = _in_source_copy(ctx, "openh264", abi, shared_src)
    toolchain, platform = ctx.toolchain, ctx.platform

    if platform == "ios":
        _hide_symbols(ctx, src)

    if platform == "windows":
        from .toolchains import WINDOWS_ABI, gas_preprocessor
        spec = WINDOWS_ABI[abi]
        # openh264 的 ARCH 用 x86_64/x86/arm64 命名（不是 VS 的 x64）；
        # x86/x64 汇编用 nasm，arm64 汇编要 gas-preprocessor.pl + armasm64，缺了就关汇编
        use_asm = "Yes" if spec["nasm"] or gas_preprocessor(toolchain.build_env(abi)) else "No"
        script = (f"make -j{ctx.jobs} OS=msvc ARCH={spec['openh264']} USE_ASM={use_asm} BUILDTYPE=Release "
                  f"install-static PREFIX={toolchain.msys_path(stage)}")
    elif platform == "android":
        from .toolchains import ANDROID_ABI
        spec = ANDROID_ABI[abi]
        # build/platform-android.mk 在 NDK_TOOLCHAIN_VERSION=clang 分支里，自己按
        # ARCH/NDKROOT/NDKLEVEL 推导 TARGET_NAME/CC/CXX/AR/SYSROOT，不需要（也不能）
        # 手动传 TOOLCHAIN_NAME/GCC_TOOLCHAIN_PATH/SYSROOT/TARGET_NAME 覆盖——
        # 命令行传入的 TARGET_NAME 优先级高于 Makefile 内的赋值，而 Makefile 自身是
        # ``CC = $(TARGET_NAME)$(NDKLEVEL)-clang``，带 API 号的 TARGET_NAME 会让
        # NDKLEVEL 被拼接两次，CC 路径变成不存在的 aarch64-linux-android2929-clang。
        script = (f"make -j{ctx.jobs} OS=android NDKROOT={toolchain.ndk} NDKLEVEL={toolchain.api} "
                  f"TARGET=android-{toolchain.api} ARCH={spec['openh264']} NDK_TOOLCHAIN_VERSION=clang "
                  f"install-static PREFIX={stage}")
    elif platform == "ohos":
        # OHOS 基于 linux 内核，openh264 走 OS=linux（编译器由 build_env 的 CC/CXX 指定）
        from .toolchains import OHOS_ABI
        script = (f"make -j{ctx.jobs} OS=linux ARCH={OHOS_ABI[abi]['openh264']} "
                  f"install-static PREFIX={stage}")
    elif platform == "linux":
        from .toolchains import LINUX_ABI
        # 汇编需要 nasm；没有就关掉（USE_ASM=No），别让编译到一半才失败
        use_asm = "Yes" if getattr(toolchain, "nasm", "") else "No"
        script = (f"make -j{ctx.jobs} OS=linux ARCH={LINUX_ABI[abi]['openh264']} "
                  f"USE_ASM={use_asm} BUILDTYPE=Release install-static PREFIX={stage}")
    elif platform == "macos":
        from .toolchains import MACOS_ABI
        # arm64 的 NEON 汇编用 clang 自带汇编器；x86_64 需要 nasm，没有就关
        use_asm = "Yes" if abi == "arm64" or getattr(toolchain, "nasm", "") else "No"
        script = (f"make -j{ctx.jobs} OS=darwin ARCH={MACOS_ABI[abi]['openh264']} "
                  f"SDK_MIN={toolchain.min_version} USE_ASM={use_asm} BUILDTYPE=Release "
                  f"install-static PREFIX={stage}")
    else:
        arch = abi.replace('-simulator', '')
        # 每个 ABI 已经是 _in_source_copy 隔离出的独立源码副本，不会再出现多 ABI
        # 共用同一份源码树互相踩到残留目标文件的问题，构建前无需再 make clean
        script = (f"make -j{ctx.jobs} OS=ios ARCH={arch} "
                  f"SDK_MIN={toolchain.min_version} install-static PREFIX={stage}")

    toolchain.sh(script, cwd=src, abi=abi, log_name=f"openh264-{abi}")
    return stage


def _hide_symbols(ctx, src: Path) -> None:
    """iOS：把符号降为 private extern，避免与其它库的 openh264 符号冲突。"""
    import re

    makefile = src / "Makefile"
    if makefile.exists():
        text = makefile.read_text(encoding="utf-8", errors="ignore")
        if "-fvisibility=hidden" not in text:
            patched = text.replace("CFLAGS += -DGENERATED_VERSION_HEADER",
                                   "CFLAGS += -DGENERATED_VERSION_HEADER -fvisibility=hidden")
            if patched != text:
                makefile.write_text(patched, encoding="utf-8")
    pattern = re.compile(r"(WELS_ASM_AARCH64_FUNC_BEGIN )(.*)")
    for asm_file in src.rglob("*.S"):
        text = asm_file.read_text(encoding="utf-8", errors="ignore")
        if ".private_extern" in text or "WELS_ASM_AARCH64_FUNC_BEGIN" not in text:
            continue
        lines = []
        for line in text.splitlines():
            match = pattern.search(line)
            if match:
                lines.append(f".private_extern _{match.group(2)}")
            lines.append(line)
        asm_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -------------------------------------------------------------------- libyuv
LIBYUV_CANDIDATES = ("yuv.lib", "libyuv.a", "yuv.a")


def build_libyuv(ctx, abi: str) -> Path:
    """libyuv 的 CMake 安装规则含工具目标，故只构建 yuv 并手动归集。"""
    _set_current("libyuv")
    src = ctx.sources.ensure(C.dep_source("libyuv", ctx.platform), ctx.source_dir("libyuv"), ctx.force_fetch)
    build_dir = ctx.work_dir("libyuv", abi) / "cmake-build"
    cmake_configure(ctx, src, build_dir, abi, extra=[
        "-DBUILD_SHARED_LIBS=OFF", "-DUNIT_TEST=OFF", "-DLIBYUV_DISABLE_JPEG=ON",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
    ])
    cmake_build(ctx, build_dir, abi, target="yuv", lib="libyuv")

    stage = ensure_dir(ctx.stage_dir("libyuv", abi))
    copy_tree(src / "include", stage / "include")
    lib_dir = ensure_dir(stage / "lib")
    built: List[Path] = []
    for name in LIBYUV_CANDIDATES:
        built = [p for p in build_dir.rglob(name) if "CMakeFiles" not in p.parts]
        if built:
            break
    if not built:
        if ctx.dry_run:              # dry-run 只打印命令，本来就没有产物
            return stage
        raise RuntimeError(f"libyuv 静态库未生成: {build_dir}")
    shutil.copy2(built[0], lib_dir / built[0].name)
    if ctx.platform == "android" and not (lib_dir / "libyuv_static.a").exists():
        shutil.copy2(lib_dir / built[0].name, lib_dir / "libyuv_static.a")
    return stage


# ---------------------------------------------------------------------- json
def build_json(ctx, abi: str) -> Path:
    """header-only：只搬头文件。

    源码包里有两个目录，语义不同，不能互相替代：
      * ``single_include/nlohmann/json.hpp`` —— 合并后的单头文件（~930KB，自包含），
        ESMediaX/3rd 里 json 就是这么放的（工程包含路径指向 single_include）。
      * ``include/nlohmann/...`` —— 模块化多头文件（46 个 .hpp），需要整体带出。
    输出哪些目录由锁定文件的 ``output_subdirs`` 决定，默认两个都给。
    （旧实现把 include 复制成两份冒充 single_include，既不是真单头也重复占空间。）
    """
    _set_current("json")
    src = ctx.sources.ensure(C.dep_source("json", ctx.platform), ctx.source_dir("json"), ctx.force_fetch)
    stage = ensure_dir(ctx.stage_dir("json", abi))
    subdirs = C.DEPS_LIBRARIES["json"].get("output_subdirs") or ["include", "single_include"]
    for sub in subdirs:
        source_sub = src / sub
        if not source_sub.exists():
            raise RuntimeError(f"json 源码缺少 {sub}: {src}")
        copy_tree(source_sub, stage / sub)
    return stage


# ------------------------------------------------------------------- spdlog
# MediaX 的 ESLogger 用 spdlog 作为日志实现（编译型静态库，非 header-only）：
# 链接方需带 SPDLOG_COMPILED_LIB（spdlog::spdlog target 会自动传播该宏）。
def build_spdlog(ctx, abi: str) -> Path:
    _set_current("spdlog")
    src = ctx.sources.ensure(C.dep_source("spdlog", ctx.platform), ctx.source_dir("spdlog"), ctx.force_fetch)
    build_dir = ctx.work_dir("spdlog", abi) / "cmake-build"
    cmake_configure(ctx, src, build_dir, abi, extra=[
        "-DBUILD_SHARED_LIBS=OFF", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DSPDLOG_INSTALL=ON",
        # 关掉示例/测试/基准，避免多编译若干可执行目标
        "-DSPDLOG_BUILD_EXAMPLE=OFF", "-DSPDLOG_BUILD_EXAMPLE_HO=OFF",
        "-DSPDLOG_BUILD_TESTS=OFF", "-DSPDLOG_BUILD_TESTS_HO=OFF",
        "-DSPDLOG_BUILD_BENCH=OFF",
        # 用 spdlog 自带 fmt，避免额外引入 fmt 依赖
        "-DSPDLOG_FMT_EXTERNAL=OFF",
    ])
    cmake_build(ctx, build_dir, abi, lib="spdlog")
    cmake_install(ctx, build_dir, abi, lib="spdlog")
    return ctx.stage_dir("spdlog", abi)


# --------------------------------------------------------------------- zlib
# FFmpeg 的 png 解码器依赖 zlib（png_decoder_select="inflate_wrapper" → zlib）：
# 没有它，水印/logo 这类 PNG 图片根本解码不了（ESMediaX 的水印加载就是踩这个）。
# 只编静态库；并补 z.lib / libz.a 别名——zlib.pc 与 ffmpeg 的 check_lib 都按 -lz 找。
def build_zlib(ctx, abi: str) -> Path:
    _set_current("zlib")
    src = ctx.sources.ensure(C.dep_source("zlib", ctx.platform), ctx.source_dir("zlib"), ctx.force_fetch)
    build_dir = ctx.work_dir("zlib", abi) / "cmake-build"
    cmake_configure(ctx, src, build_dir, abi, extra=[
        "-DZLIB_BUILD_SHARED=OFF",      # 只要静态库，避免多出一个 zlib1.dll
        "-DZLIB_BUILD_STATIC=ON",
        "-DZLIB_BUILD_TESTING=OFF",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
    ])
    cmake_build(ctx, build_dir, abi, target="zlibstatic", lib="zlib")
    cmake_install(ctx, build_dir, abi, lib="zlib")

    stage = ctx.stage_dir("zlib", abi)
    lib_dir = ensure_dir(stage / "lib")
    for built in sorted(lib_dir.glob("*.lib")) + sorted(lib_dir.glob("*.a")):
        for alias in (("z.lib", "zlib.lib") if built.suffix == ".lib" else ("libz.a",)):
            target = lib_dir / alias
            if not target.exists():
                shutil.copy2(built, target)
    return stage


# -------------------------------------------------------------------- ffmpeg
# ffmpeg 采用 out-of-tree 构建（configure 在各 profile 独立的 build 目录里跑），
# 但源码树若残留**历史 in-source 构建**产物就会被静默误用：
#   libavfilter/allfilters.c 里是 `#include "libavfilter/filter_list.c"`（引号包含），
#   编译器优先搜索**被包含文件所在目录** → 命中源码树里的旧 filter_list.c；
#   结果新滤镜虽编译出 af_volume.o/af_amix.o/af_asetnsamples.o，却没登记进
#   filter_list[]，运行期 `ffmpeg -filters` 仍是旧列表，开播滤镜链报
#   "No option name near '1.0'"/找不到 volume/amix。
#   codec_list/muxer_list/demuxer_list/protocol_list/bsf_list/parser_list 同理。
# 组件表（*_list.c）是被下面这些 .c 用**引号**包含进去的，而生成表不参与依赖跟踪
# （MSVC 的 .d 里没有它），所以增量编译时会出现"表已更新、目标文件没重编"：
# 补了 extract_extradata 之后 `ffmpeg -bsfs` 依旧列不出来，就是 bitstream_filters.o 没重编。
# 每次构建前删掉这几个目标文件，强制按新表重编（代价只有 6 个文件）。
FFMPEG_LIST_CONSUMERS = {
    "libavcodec": ("allcodecs.o", "bitstream_filters.o", "parsers.o"),
    "libavfilter": ("allfilters.o",),
    "libavformat": ("allformats.o", "protocols.o"),
}


def drop_component_list_objects(build_dir: Path, logger) -> None:
    dropped = 0
    for subdir, names in FFMPEG_LIST_CONSUMERS.items():
        for name in names:
            target = build_dir / subdir / name
            if target.exists():
                target.unlink()
                dropped += 1
    if dropped:
        logger.debug(f"使 {dropped} 个组件表目标文件失效（强制按最新 *_list.c 重编）")


FFMPEG_GENERATED_FILES = (
    # configure 产物：源码树里的 config.h/config.asm 会被引号包含优先命中
    "config.h", "config.asm", "config.mak", "config.texi", "version.h", "ffversion.h",
    ".version", ".config",
    # 组件表（*_list.c）
    "libavcodec/bsf_list.c", "libavcodec/codec_list.c", "libavcodec/parser_list.c",
    "libavdevice/indev_list.c", "libavdevice/outdev_list.c",
    "libavfilter/filter_list.c",
    "libavformat/demuxer_list.c", "libavformat/muxer_list.c", "libavformat/protocol_list.c",
    # ffbuild/ 本身是受版本控制的源码目录（common.mak 等），只清里面的 configure 产物
    "ffbuild/config.mak", "ffbuild/config.sh", "ffbuild/config.log",
    "ffbuild/config.fate", "ffbuild/.config",
)


def _git_clean(src: Path, dry_run: bool) -> int:
    cmd = ["git", "-C", str(src), "clean", "-xfd", "-e", ".deps_pin"]
    if dry_run:
        cmd.append("-n")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git clean 失败: {proc.stderr.strip()}")
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def prune_source_tree(src: Path, logger) -> bool:
    """清掉源码树内历史构建残留，返回是否发生过清理。

    优先走 ``git clean -xfd``：一次性、彻底，且天然不会误删受版本控制的源码
    （ffbuild/、tests/ 里都混着 tracked 文件，按目录删会毁掉源码）。
    非 git 源码树（解压的 tarball）退化为删除已知生成文件。
    ``.deps_pin`` 是源码管理器的锁定标记，必须保留，否则每次都会重新克隆。
    """
    if (src / ".git").exists():
        pending = _git_clean(src, dry_run=True)
        if not pending:
            return False
        _git_clean(src, dry_run=False)
        logger.info(f"清理 ffmpeg 源码树内 {pending} 项历史构建残留"
                    f"（残留的 *_list.c / config.h 会覆盖新配置）: {src}")
        return True
    removed = 0
    for rel in FFMPEG_GENERATED_FILES:
        target = src / rel
        if target.is_file():
            target.unlink()
            removed += 1
    if removed:
        logger.info(f"清理 ffmpeg 源码树内 {removed} 项历史构建残留"
                    f"（残留的 *_list.c / config.h 会覆盖新配置）: {src}")
    return removed > 0


def remove_tree(path: Path) -> None:
    """删目录：走系统命令（海量 .o 逐个 unlink 既慢又容易被批量删除保护拦截）。"""
    path = Path(path)
    if not path.exists():
        return
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(path)], check=False)
    else:
        subprocess.run(["rm", "-rf", str(path)], check=False)
    if path.exists():                       # 兜底
        shutil.rmtree(path, ignore_errors=True)


def _warn_if_deps_are_broken(ctx, build_dir: Path) -> None:
    """构建后体检：ffmpeg 的 ``.d`` 依赖文件是否生成成功。

    空 ``.d`` 不会让本次构建出错（配置一致性已由 ``_ensure_build_dir`` 兜底），
    但意味着 make 感知不到头文件变化 —— 增量构建时改头文件不会触发重编。
    静默存在容易让人误判"改了配置怎么没生效"，所以明确告警一次。
    """
    if ctx.platform != "windows":
        return
    total = empty = 0
    for dep in build_dir.rglob("*.d"):
        total += 1
        try:
            if dep.stat().st_size == 0:
                empty += 1
        except OSError:
            continue
    if total and empty * 2 > total:
        ctx.logger.warn(f"ffmpeg 依赖文件不可用（{empty}/{total} 个 .d 为空）：make 感知不到"
                        f" config.h/头文件变化，增量构建可能不重编。"
                        f" 根治办法：给 VS 装 English Language Pack（或用 VSLANG=1033 生效的环境）；"
                        f" 当前靠 configure 指纹兜底（参数变了会全量重编），必要时用 --clean")


def _ensure_build_dir(work_dir: Path, configure_args: List[str], ctx) -> Path:
    """按 configure 命令行做指纹：变了就重建中间目录（全量重编）。

    保险丝。ffmpeg 靠 ``.d`` 依赖文件感知 ``config.h`` 变化，而 MSVC 下这些依赖
    文件经常是 0 字节（cl 的 ``-showIncludes`` 输出被中文本地化，ffbuild/common.mak
    的 sed 只认英文 ``Note: including file:``，见 toolchains.py 的 ``VSLANG``）。
    依赖失效时 make 只会重编"缺失 / 被强制失效"的目标，于是出现
    **配置改了、产物却只更新了一半**（编码器已进 DLL，ffmpeg.exe 里打印的
    configuration 串还是旧的）。用命令行指纹兜底，保证"配置即产物"。
    """
    marker = work_dir / ".configure_args"
    current = "\n".join(configure_args)
    previous = ""
    if marker.is_file():
        try:
            previous = marker.read_text(encoding="utf-8")
        except OSError:
            previous = ""
    # --dry-run 只是"看看会执行什么"，绝不能顺手把中间目录删掉（曾经就是这么踩的）
    dry_run = bool(getattr(ctx, "dry_run", False)
                   or getattr(getattr(ctx, "runner", None), "dry_run", False))
    if previous and previous != current:
        ctx.logger.info("ffmpeg configure 参数变了，重建中间目录（全量重编）："
                        "避免 make 只重编一部分导致配置与产物不一致")
        if not dry_run:
            remove_tree(work_dir)
    build_dir = ensure_dir(work_dir / "build")
    if not dry_run:
        try:
            marker.write_text(current, encoding="utf-8")
        except OSError:
            pass
    return build_dir


# ---------------------------------------------------------------- 白名单校验
# configure 对 --enable-demuxer=mkv 这种**不存在的组件名**只打一行 WARNING 然后忽略，
# 产物里静默缺组件，要到运行期打开文件才暴露（交叉编译平台连构建后的自检都跑不了）。
# 组件名的唯一来源是源码里的登记表，与 configure 自己的 find_things_extern 同一套规则。
_COMPONENT_REGISTRY = {
    # 白名单键: (登记文件, 正则 —— 第 1 组为 configure 使用的组件名)
    "encoders": ("libavcodec/allcodecs.c", r"^extern const \w+ ff_(\w+)_encoder;"),
    "decoders": ("libavcodec/allcodecs.c", r"^extern const \w+ ff_(\w+)_decoder;"),
    "parsers": ("libavcodec/parsers.c", r"^extern const \w+ ff_(\w+)_parser;"),
    "bsfs": ("libavcodec/bitstream_filters.c", r"^extern const \w+ ff_(\w+)_bsf;"),
    "muxers": ("libavformat/allformats.c", r"^extern const \w+ +ff_(\w+)_muxer;"),
    "demuxers": ("libavformat/allformats.c", r"^extern const \w+ +ff_(\w+)_demuxer;"),
    "protocols": ("libavformat/protocols.c", r"^extern const \w+ ff_(\w+)_protocol;"),
    "filters": ("libavfilter/allfilters.c", r"^extern const \w+ ff_(?:[avfsinkrc]{2,5})_(\w+);"),
}


def ffmpeg_component_names(src: Path, key: str) -> Optional[set]:
    """源码登记的某类组件名集合；登记文件不存在（源码未就绪 / dry-run）返回 None。"""
    rel, pattern = _COMPONENT_REGISTRY[key]
    path = Path(src) / rel
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    return set(re.findall(pattern, text, flags=re.MULTILINE))


def validate_whitelist(ctx, src: Path) -> None:
    """白名单里每个名字都必须是该 ffmpeg 源码里真实存在的组件，否则 configure 前就报错。"""
    profile = getattr(ctx, "profile", "")
    wl = C.whitelist(profile)
    wanted = {key: list(wl.get(key) or []) for key in _COMPONENT_REGISTRY}
    wanted["encoders"] = C.whitelist_encoders(ctx.platform, profile, ctx.enable_nvenc, ctx.enable_amf)
    wanted["bsfs"] = C.whitelist_bsfs(profile)
    problems = []
    for key, names in wanted.items():
        known = ffmpeg_component_names(src, key)
        if known is None:
            continue
        if not known:
            # 登记表格式变了（换了 ffmpeg 大版本 / fork）：宁可跳过也不误报
            ctx.logger.warn(f"白名单校验跳过 {key}：{_COMPONENT_REGISTRY[key][0]} 里没解析出任何组件名")
            continue
        unknown = [n for n in names if n and n not in known]
        if unknown:
            problems.append(f"{key}: {', '.join(unknown)}")
    if problems:
        raise RuntimeError(
            "ffmpeg 裁剪白名单里有源码中不存在的组件名（configure 会静默忽略，产物缺组件）：\n  "
            + "\n  ".join(problems)
            + "\n  请改用 configure 的内部名（如 mp4/m4a → mov，mkv/webm → matroska，srt 协议 → libsrt），"
              "见 ffmpeg_build/config.py 的 PROFILE_WHITELIST")


def build_ffmpeg(ctx, abi: str) -> Path:
    _set_current("ffmpeg")
    stage = ensure_dir(ctx.stage_dir("ffmpeg", abi))
    lib = C.LIBRARIES["ffmpeg"]

    # 个别平台使用专用源码（见锁定文件各条目的 platform_overrides）：
    # OHOS 用 openharmony-tpc 的 fork；Linux 上内网不可达时用上游镜像
    source = C.dep_source("ffmpeg", ctx.platform)
    # 允许 --ffmpeg-ref 覆盖仓库分支/标签
    if ctx.ffmpeg_ref and ctx.ffmpeg_ref != source.ref:
        from dataclasses import replace
        from .utils import GitSource
        source = replace(source, ref=ctx.ffmpeg_ref)
    src = ctx.sources.ensure(source, ctx.source_dir("ffmpeg"), ctx.force_fetch)
    pruned = prune_source_tree(src, ctx.logger)
    validate_whitelist(ctx, src)

    prefixes = _dependency_prefixes(ctx, abi)
    args = _ffmpeg_common_args(ctx, abi, stage, prefixes)
    args += _ffmpeg_platform_args(ctx, abi)

    env = ctx.toolchain.build_env(abi)
    separator = getattr(ctx.toolchain, "shell_path_separator", os.path.sep)
    to_path = ctx.toolchain.msys_path if ctx.platform == "windows" else str
    work_dir = ctx.work_dir("ffmpeg", abi)
    pkg_paths = [to_path(prefix / "lib" / "pkgconfig") for prefix in prefixes]
    if ctx.enable_nvenc:
        # nvenc 走 check_pkg_config：必须有带版本号的 ffnvcodec.pc，光给头文件 configure 会失败
        pc_dir = hw_sdk_pkgconfig_dir(ctx, "nv-codec-headers", abi)
        if pc_dir is not None:
            pkg_paths.append(to_path(pc_dir))
    if env.get("PKG_CONFIG_PATH"):
        pkg_paths.append(env["PKG_CONFIG_PATH"])
    pkg_config_path = separator.join(pkg_paths)
    ctx.logger.debug(f"PKG_CONFIG_PATH={pkg_config_path}")

    # PKG_CONFIG_PATH 必须在 shell 内 export：外部传入会被 MSYS 做路径转换而失效
    # 必须**源码外**构建：ffmpeg 是 in-source 编译，两个 profile 共用 sources/ffmpeg 时
    # 会互相踩到对方的 config.h 与 .o（曾出现 player 链接到 capture 的 random_seed.o、
    # 报 RAND_bytes 未解析）。放到 <profile>-<platform>-<abi>/ffmpeg/build 里各自独立。
    if pruned:
        # 源码树刚被清理：旧目标文件对应的组件表已失效，且 allfilters.o/allcodecs.o
        # 只依赖同名 .c（不含 *_list.c），make 不会自动重编 → 必须重建中间目录
        ctx.logger.info("源码树有残留被清理，重建 ffmpeg 中间目录（全量重编）")
        remove_tree(work_dir)
    build_dir = _ensure_build_dir(work_dir, args, ctx)
    drop_component_list_objects(build_dir, ctx.logger)
    script = (f"export PKG_CONFIG_PATH='{pkg_config_path}' && {to_path(src / 'configure')} "
              + " ".join(args) + f" && make -j{ctx.jobs} && make install")
    ctx.toolchain.sh(script, cwd=build_dir, abi=abi, env=env, log_name=f"ffmpeg-{abi}")
    _warn_if_deps_are_broken(ctx, build_dir)

    if ctx.platform == "windows":
        # MSVC 下 import lib 装在 bin/，同步一份到 lib/
        bin_dir, lib_dir = stage / "bin", ensure_dir(stage / "lib")
        for import_lib in bin_dir.glob("*.lib"):
            if not (lib_dir / import_lib.name).exists():
                shutil.copy2(import_lib, lib_dir / import_lib.name)
    # 本机平台的产物能直接跑，顺手体检一次；交叉编译产物（含 x64 宿主上的 Windows arm64）跳过
    if ctx.platform in NATIVE_VERIFY_PLATFORMS and _runs_on_host(ctx, abi):
        _verify_components(ctx, stage)
    return stage


def _runs_on_host(ctx, abi: str) -> bool:
    if ctx.platform == "windows":
        from .toolchains import WINDOWS_ABI, windows_host_arch
        # x64 宿主能跑 x86（WOW64），跑不了 arm64；arm64 宿主三种都能跑（仿真）
        return WINDOWS_ABI[abi]["target"] != "arm64" or windows_host_arch() == "arm64"
    return True


# 运行期组件自检：白名单里写了、但二进制没登记（GPL 冲突/依赖缺失/组件表被旧文件覆盖）
# 都会静默发生，只有跑起来才暴露（如开播滤镜链找不到 volume/amix）。
# 只在"产物能直接在宿主执行"的平台上跑（Windows / Linux 本机）；
# 交叉编译产物（android/ios/ohos）在宿主上跑不起来，只能跳过。
NATIVE_VERIFY_PLATFORMS = ("windows", "linux", "macos")
_COMPONENT_CHECKS = (("filters", "-filters"), ("decoders", "-decoders"),
                     ("encoders", "-encoders"), ("demuxers", "-demuxers"),
                     ("muxers", "-muxers"), ("protocols", "-protocols"),
                     ("bsfs", "-bsfs"))
# 白名单用的是 configure 的内部名，ffmpeg CLI 对外显示名可能不同
_COMPONENT_ALIASES = {"protocols": {"libsrt": "srt"},
                      "demuxers": {"image_png_pipe": "png_pipe"}}


def _verify_components(ctx, stage: Path) -> None:
    bin_dir = stage / "bin"
    exe = None
    for name in ("ffmpeg.exe", "ffmpeg"):       # Windows 带 .exe，Linux 不带
        candidate = bin_dir / name
        if candidate.is_file():
            exe = candidate
            break
    if exe is None:
        return
    wl = C.whitelist(getattr(ctx, "profile", ""))
    wl = dict(wl, encoders=C.whitelist_encoders(ctx.platform, getattr(ctx, "profile", ""),
                                                ctx.enable_nvenc, ctx.enable_amf),
              bsfs=C.whitelist_bsfs(getattr(ctx, "profile", "")))
    try:
        for key, flag in _COMPONENT_CHECKS:
            aliases = _COMPONENT_ALIASES.get(key, {})
            wanted = [aliases.get(n, n) for n in (wl.get(key) or []) if n]
            if not wanted:
                continue
            text = subprocess.run([str(exe), "-hide_banner", flag], capture_output=True,
                                  text=True, errors="replace", timeout=60).stdout
            missing = [n for n in wanted
                       if not re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]
            if missing:
                ctx.logger.warn(f"[{key}] {len(missing)} 个组件白名单项未编进产物: "
                                f"{', '.join(missing)}")
    except (OSError, subprocess.SubprocessError) as exc:      # 自检失败不影响构建结果
        ctx.logger.debug(f"组件自检跳过: {exc}")


FFMPEG_DEPENDENCIES = C.FFMPEG_DEPENDENCIES   # 来自锁定文件的 ffmpeg_deps 组（player 为空）


def _dependency_prefixes(ctx, abi) -> List[Path]:
    if not FFMPEG_DEPENDENCIES:      # 播放链路：ffmpeg 不带任何外部库
        return []
    ready, missing = [], []
    for name in FFMPEG_DEPENDENCIES:
        prefix = ctx.install_dir(name, abi)
        if (prefix / "include").exists() and (prefix / "lib").exists():
            ready.append(prefix)
        else:
            missing.append(str(prefix))
    if missing:
        raise RuntimeError(
            "ffmpeg 依赖未就绪，请先编译: python build.py build "
            f"--platform {ctx.platform} --libs {','.join(FFMPEG_DEPENDENCIES)}\n  缺失: {', '.join(missing)}")
    return ready


def prepare_hw_sdk(ctx, name: str, abi: str) -> Path:
    """按锁定文件拉取桌面硬编 SDK，装到产物目录，返回可直接喂给 ``-I`` 的 include 目录。

    头文件位置的取舍（沿用各 SDK 官方 ``make install`` 的布局，放在**自己的库目录**下）：

        <输出>/amf/windows/include/AMF/...                     （AMD AMF SDK 官方布局）
        <输出>/nv-codec-headers/windows/include/ffnvcodec/*.h  （nv-codec-headers 官方布局）
        <输出>/nv-codec-headers/windows/lib/pkgconfig/ffnvcodec.pc
        <输出>/<sdk>/windows/version-...txt                    （版本标记，可追溯 commit）

    三条原则：
      1. **不放进 ffmpeg/ 目录**——之前正是塞在 ffmpeg 的 include 里，整目录覆盖时被一起删掉；
      2. 与 zlib/openssl 等库同一套 ``<库>/<平台>/{include,lib}`` 约定（平台层靠 platforms 限定为 windows）；
      3. 上游仓库的目录结构不一定符合 ffmpeg 的写法（AMF 仓库里是
         ``amf/public/include/{core,components}``，而 ffmpeg 写的是
         ``#include <AMF/core/Version.h>``），安装时按 ``header_prefix`` 规整出
         ``AMF/`` 这一层；上游源码目录保持原样不动。
    """
    spec = C.hw_sdk(name, ctx.platform)
    if spec is None:
        raise RuntimeError(f"{name}: 锁定文件未定义，或当前平台（{ctx.platform}）不使用该 SDK"
                           f"（platforms={C.HW_SDKS.get(name, {}).get('platforms')}）")
    src = ctx.sources.ensure(C.SDK_SOURCES[name], ctx.source_dir(name), ctx.force_fetch)
    # 头文件与架构无关 -> 用 flat（不带 abi 层），多 ABI 构建共用同一份 <sdk>/<平台>/include
    dest = ctx.install_dir(name)
    include = ensure_dir(dest / "include")

    include_src = src / (spec.get("include_dir") or ".")
    prefix = spec.get("header_prefix") or ""
    target = include / prefix if prefix else include
    shutil.rmtree(target, ignore_errors=True)
    copy_tree(include_src, target)

    marker = spec.get("marker") or ""
    if marker and not (include / marker).is_file():
        raise RuntimeError(f"{name}: SDK 头文件布局不对，{include} 下找不到 {marker}")

    # nv-codec-headers 官方会在 $(PREFIX)/lib/pkgconfig 装 ffnvcodec.pc（ffmpeg 靠它定位 SDK）
    template_name = spec.get("pkgconfig_template") or ""
    if template_name:
        template = src / template_name
        if template.is_file():
            pc_dir = ensure_dir(dest / "lib" / "pkgconfig")
            content = template.read_text(encoding="utf-8").replace("@@PREFIX@@", dest.as_posix())
            (pc_dir / "ffnvcodec.pc").write_text(content, encoding="utf-8")

    _write_sdk_marker(ctx, name, spec, dest, abi)
    ctx.logger.info(f"{name} SDK -> {dest}")
    return include


def hw_sdk_pkgconfig_dir(ctx, name: str, abi: str) -> Optional[Path]:
    """硬编 SDK 装好后的 pkgconfig 目录（ffmpeg 用 check_pkg_config 找 ffnvcodec）。"""
    spec = C.hw_sdk(name, ctx.platform) or {}
    if not spec.get("pkgconfig_template"):
        return None
    candidate = ctx.install_dir(name) / "lib" / "pkgconfig"
    return candidate if candidate.is_dir() else None


def _write_sdk_marker(ctx, name: str, spec: dict, dest: Path, abi: str) -> None:
    """给 SDK 也写一份版本标记（与其它库同款文件名规则），便于追溯 commit/版本。"""
    import platform as pl
    from datetime import datetime
    from .utils import build_marker_name, git_head_sha
    commit = git_head_sha(ctx.source_dir(name))
    now = datetime.now()
    for stale in dest.glob("version-*.txt"):
        stale.unlink()
    marker = build_marker_name(name, spec.get("version") or "-", ctx.platform, abi,
                               "header-only", commit=commit, when=now,
                               variant=getattr(ctx.toolchain, "marker_variant", ""))
    write_version_file(dest / marker, [
        f"library    : {name}",
        f"version    : {spec.get('version') or '-'}",
        f"commit     : {commit or '-'}",
        f"group      : {C.HW_SDK_GROUP}（构建期输入，非运行时库）",
        f"summary    : {spec.get('note', '')}",
        f"platform   : {ctx.platform} ({', '.join(ctx.abis)})",
        f"platforms  : {', '.join(spec.get('platforms') or [])}",
        f"compiler   : header-only",
        f"layout     : {ctx.toolchain.layout_of(name)}",
        f"build time : {now.isoformat(timespec='seconds')}",
        f"build host : {pl.system()} {pl.machine()}",
        f"source     : {ctx.source_dir(name)}",
    ])


def _ffmpeg_common_args(ctx, abi, stage: Path, prefixes) -> List[str]:
    profile = getattr(ctx, "profile", "")
    wl = C.whitelist(profile)
    encoders = C.whitelist_encoders(ctx.platform, profile, ctx.enable_nvenc, ctx.enable_amf)

    def enable(option: str, key: str, values=None) -> List[str]:
        items = list(values) if values is not None else list(wl.get(key) or [])
        return [f"--enable-{option}={','.join(items)}"] if items else []

    # Windows 上 make 经 MSYS bash 执行，配方里的 -ID:\a\b 会被 shell 当转义吃掉
    # （反斜杠被删除 -> -ID:ab），cl.exe 就找不到头；统一输出 D:/... 正斜杠形式，
    # 与 pkg-config 给出的路径同款，cl.exe / link.exe 原生可识别。
    to_include = (lambda p: Path(p).as_posix()) if ctx.platform == "windows" else str
    includes = " ".join(f"-I{to_include(p)}/include" for p in prefixes)
    # 硬编 SDK：按锁定文件拉取并装到 <输出>/<sdk>/<平台>/include，再经 -I 参与 configure
    if ctx.enable_nvenc:
        includes += f" -I{to_include(prepare_hw_sdk(ctx, 'nv-codec-headers', abi))}"
    if ctx.enable_amf:
        # configure 用 check_cpp_condition 验 AMF/core/Version.h，SDK 头必须在搜索路径里
        includes += f" -I{to_include(prepare_hw_sdk(ctx, 'amf', abi))}"
    if ctx.platform == "windows":
        libs = " ".join(f"-LIBPATH:{to_include(p)}/lib" for p in prefixes)
    else:
        libs = " ".join(f"-L{p}/lib" for p in prefixes)
        includes += " -ffunction-sections -fdata-sections"   # 配合最终链接的 --gc-sections

    to_path = ctx.toolchain.msys_path if ctx.platform == "windows" else str
    return [
        f"--prefix='{to_path(stage)}'",
        "--pkg-config-flags=--static",
        # 组件白名单（按 profile：capture 编码+推流 / player 解码+渲染）
        "--disable-everything",
        "--disable-autodetect",
        *enable("encoder", "encoders", encoders),
        *enable("decoder", "decoders"),
        *enable("muxer", "muxers"),
        *enable("demuxer", "demuxers"),
        *enable("parser", "parsers"),
        # bsf 用"白名单 + avformat 内部自动插入的过滤器"：后者裁掉只在运行期炸
        *enable("bsf", "bsfs", C.whitelist_bsfs(getattr(ctx, "profile", ""))),
        *enable("filter", "filters"),
        *enable("protocol", "protocols"),
        # 外部库：开关来自锁定文件 ffmpeg_deps 组的 ffmpeg_flag（player 为空）
        *C.FFMPEG_DEP_FLAGS,
        "--enable-nonfree", "--enable-version3",
        # 关掉用不到的部分
        # 注：保留 programs（ffmpeg/ffprobe CLI）用于验证产物可用性，
        #     它们是独立可执行文件，不进入 SDK 链接，对库体积无影响。
        "--disable-avdevice", "--disable-postproc", "--disable-vulkan",
        "--disable-doc", "--disable-htmlpages",
        "--disable-manpages", "--disable-podpages", "--disable-txtpages",
        "--disable-debug", "--disable-iconv",
        "--enable-small", "--enable-pic",
        f"--extra-cflags='{includes}'",
        f"--extra-ldflags='{libs}'",
    ] + (["--enable-lto"] if ctx.ffmpeg_lto else [])


def _ffmpeg_platform_args(ctx, abi) -> List[str]:
    if ctx.platform == "windows":
        from .toolchains import WINDOWS_ABI, gas_preprocessor, windows_host_arch
        spec = WINDOWS_ABI[abi]
        args = [f"--target-os={spec['ffmpeg_os']}", f"--arch={spec['ffmpeg_arch']}", "--toolchain=msvc",
                "--enable-shared", "--disable-static",
                # openssl 在 Windows 不装 .pc，静态链接所需系统库需显式给出
                "--extra-libs='-lws2_32 -lgdi32 -lcrypt32 -ladvapi32 -luser32'"]
        if spec["target"] != windows_host_arch() and spec["target"] == "arm64":
            # x64 宿主跑不了 arm64 的测试程序；x86 产物 x64 宿主能直接跑，不算交叉编译
            args.append("--enable-cross-compile")
        if not spec["nasm"] and not gas_preprocessor(ctx.toolchain.build_env(abi)):
            # MSVC 编 ARM64 汇编要 gas-preprocessor.pl 转成 armasm64 语法，缺了就整体关汇编
            args.append("--disable-asm")
        # 硬编开关与附加开关都来自锁定文件（extra_flags 里带 --enable-d3d11va：
        # AMF 走 D3D11，采集链路本就是 D3D11/DXGI，直传显存帧时必须有它）
        for enabled, name, fallback in ((ctx.enable_nvenc, "nv-codec-headers", "--enable-nvenc"),
                                        (ctx.enable_amf, "amf", "--enable-amf")):
            if not enabled:
                continue
            spec = C.hw_sdk(name, ctx.platform) or {}
            args.append(spec.get("ffmpeg_flag") or fallback)
            # extra_flags：--disable-everything 会把外部库(ffnvcodec)与 hwaccel(d3d11va)
            # 一起关掉，必须显式补回来，否则硬编的依赖永远满足不了
            args.extend(spec.get("extra_flags") or [])
        return args

    if ctx.platform == "android":
        from .toolchains import ANDROID_ABI
        env = ctx.toolchain.build_env(abi)
        bin_dir = ctx.toolchain.toolchain_bin
        return ["--target-os=android", f"--arch={ANDROID_ABI[abi]['arch']}", "--enable-cross-compile",
                f"--cc='{env['CC']}'", f"--cxx='{env['CXX']}'",
                f"--ar='{bin_dir / 'llvm-ar'}'", f"--nm='{bin_dir / 'llvm-nm'}'",
                f"--ranlib='{bin_dir / 'llvm-ranlib'}'", f"--strip='{bin_dir / 'llvm-strip'}'",
                f"--sysroot='{env['SYSROOT']}'",
                "--enable-jni", "--enable-mediacodec",
                "--enable-static", "--disable-shared", "--disable-symver",
                "--extra-cflags=-fPIC", "--extra-libs=-lm"]

    if ctx.platform == "ohos":
        from .toolchains import OHOS_ABI
        spec = OHOS_ABI[abi]
        bin_dir = ctx.toolchain.llvm_bin
        args = ["--target-os=linux", f"--arch={spec['arch']}", "--enable-cross-compile",
                f"--cc='{bin_dir / (spec['triple'] + '-clang')}'",
                f"--cxx='{bin_dir / (spec['triple'] + '-clang++')}'",
                f"--ar='{bin_dir / 'llvm-ar'}'", f"--nm='{bin_dir / 'llvm-nm'}'",
                f"--ranlib='{bin_dir / 'llvm-ranlib'}'", f"--strip='{bin_dir / 'llvm-strip'}'",
                f"--sysroot='{ctx.toolchain.sysroot}'",
                "--disable-x86asm", "--disable-symver",
                "--enable-static", "--disable-shared", "--extra-libs=-lm"]
        if spec.get("neon"):
            args.append("--enable-neon")
        args += list(C.OHOS_FFMPEG_EXTRA_ARGS)
        return args

    if ctx.platform == "linux":
        from .toolchains import LINUX_ABI
        # 本机编译：不需要 --enable-cross-compile / --sysroot，直接用宿主 gcc；
        # 产物为静态库（与 Android/iOS 一致），方便消费方直接链进自己的可执行文件
        args = [f"--arch={LINUX_ABI[abi]['arch']}", "--target-os=linux",
                "--enable-static", "--disable-shared", "--disable-symver",
                # openssl 与 srt 都是静态库，系统侧依赖（m/pthread/dl/stdc++）要显式补上
                "--extra-libs='-lm -lpthread -ldl -lstdc++'"]
        if not getattr(ctx.toolchain, "nasm", ""):
            # ffmpeg 6.1 的 x86 汇编要求 nasm >= 2.13；没有就整体关掉而不是编到一半失败
            args.append("--disable-x86asm")
        return args

    if ctx.platform == "macos":
        from .toolchains import MACOS_ABI
        toolchain = ctx.toolchain
        flags = toolchain.arch_flags(abi)
        args = [f"--arch={MACOS_ABI[abi]['arch']}", "--target-os=darwin",
                "--cc=clang", "--cxx=clang++",
                f"--sysroot='{toolchain.sdk_path}'",
                "--enable-videotoolbox",
                "--enable-static", "--disable-shared", "--disable-symver",
                f"--extra-cflags='{flags} -ffunction-sections -fdata-sections'",
                f"--extra-ldflags='{flags} -framework CoreFoundation -framework CoreVideo "
                "-framework CoreMedia -framework VideoToolbox -framework Security'",
                "--extra-libs='-lc++'"]
        if abi == "x86_64" and not getattr(toolchain, "nasm", ""):
            args.append("--disable-x86asm")
        return args

    toolchain = ctx.toolchain
    arch = abi.replace("-simulator", "")
    sim = toolchain.sdk_name(abi) == "iphonesimulator"
    min_flag = (f"-mios-simulator-version-min={toolchain.min_version}" if sim
                else f"-miphoneos-version-min={toolchain.min_version}")
    return ["--target-os=darwin", f"--arch={arch}", "--enable-cross-compile",
            f"--cc='xcrun -sdk {toolchain.sdk_name(abi)} clang'",
            f"--cxx='xcrun -sdk {toolchain.sdk_name(abi)} clang++'",
            f"--sysroot='{toolchain.sdk_path(abi)}'",
            "--disable-asm",                       # iOS 下 ffmpeg 汇编需要 gas-preprocessor
            "--enable-videotoolbox",
            "--enable-static", "--disable-shared", "--disable-symver",
            f"--extra-cflags='-arch {arch} {min_flag} -ffunction-sections -fdata-sections'",
            f"--extra-ldflags='-arch {arch} {min_flag} -framework CoreFoundation "
            "-framework CoreVideo -framework CoreMedia -framework VideoToolbox'"]


REGISTRY = {
    "zlib": build_zlib,
    "openssl": build_openssl,
    "srt": build_srt,
    "fdk-aac": build_fdk_aac,
    "openh264": build_openh264,
    "libyuv": build_libyuv,
    "json": build_json,
    "spdlog": build_spdlog,
    "ffmpeg": build_ffmpeg,
}
