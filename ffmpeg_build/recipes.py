"""各依赖库的编译配方。

每个配方都是 ``build_xxx(ctx, abi) -> stage 目录``，在 :data:`REGISTRY` 中注册；
新增一个库只需在这里加一个函数 + 在 config.py 登记。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional

from . import config as C
from .utils import copy_tree, ensure_dir, which

# 当前正在构建的库名（供 cmake_* 辅助函数取 install prefix 与日志名）
CURRENT_LIB = {"name": ""}


def _set_current(lib: str) -> None:
    CURRENT_LIB["name"] = lib


# ----------------------------------------------------------------- CMake 辅助
def cmake_configure(ctx, src: Path, build_dir: Path, abi: str, extra: Optional[List[str]] = None) -> None:
    build_dir = Path(build_dir)
    ensure_dir(build_dir)
    cmd = ["cmake", "-S", str(src), "-B", str(build_dir)]
    cmd += ctx.toolchain.cmake_args(abi)
    cmd += [f"-DCMAKE_INSTALL_PREFIX={ctx.stage_dir(CURRENT_LIB['name'], abi)}",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"]   # CMake 4 需要，兼容老项目(如 srt)
    cmd += list(extra or [])
    ctx.runner.run(cmd, cwd=src, env=ctx.toolchain.build_env(abi), log_name=f"{CURRENT_LIB['name']}-{abi}-cmake")


def cmake_build(ctx, build_dir: Path, abi: str, target: Optional[str] = None, lib: str = "") -> None:
    cmd = ["cmake", "--build", str(build_dir)]
    if getattr(ctx.toolchain, "needs_config_type", False):
        cmd += ["--config", "Release"]
    if target:
        cmd += ["--target", target]
    cmd += ["--parallel", str(ctx.jobs)]
    ctx.runner.run(cmd, cwd=build_dir, env=ctx.toolchain.build_env(abi), log_name=f"{lib}-{abi}-build")


def cmake_install(ctx, build_dir: Path, abi: str, lib: str = "") -> None:
    cmd = ["cmake", "--install", str(build_dir)]
    if getattr(ctx.toolchain, "needs_config_type", False):
        cmd += ["--config", "Release"]
    ctx.runner.run(cmd, cwd=build_dir, env=ctx.toolchain.build_env(abi), log_name=f"{lib}-{abi}-install")


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
    src = ctx.sources.ensure(C.LIBRARIES["openssl"].source, ctx.source_dir("openssl"), ctx.force_fetch)
    toolchain, platform = ctx.toolchain, ctx.platform

    if platform == "windows":
        env = toolchain.build_env(abi)
        perl = _native_perl(toolchain)
        if not perl:
            raise RuntimeError("openssl 需要原生 Windows perl，执行: python build.py bootstrap --platform windows --install")
        nmake = which("nmake", env)
        if not nmake:
            raise RuntimeError("openssl 需要 MSVC 的 nmake")
        prefix = str(stage).replace("\\", "/")
        ctx.runner.run([perl, "Configure", "VC-WIN64A", *OPENSSL_OPTIONS,
                        f"--prefix={prefix}", f"--openssldir={prefix}/ssl"],
                       cwd=src, env=env, log_name="openssl-configure")
        ctx.runner.run([nmake], cwd=src, env=env, log_name="openssl-build")
        ctx.runner.run([nmake, "install_sw"], cwd=src, env=env, log_name="openssl-install")
    else:
        if platform == "android":
            from .toolchains import ANDROID_ABI
            target = ANDROID_ABI[abi]["openssl"]
            configure = (f"./Configure {target} -D__ANDROID_API__={toolchain.api} "
                         f"{' '.join(OPENSSL_OPTIONS)} --prefix={stage} --openssldir={stage}/ssl")
        else:
            configure = (f"./Configure {IOS_OPENSSL_TARGETS.get(abi, 'ios64-xcrun')} "
                         f"{' '.join(OPENSSL_OPTIONS)} --prefix={stage} --openssldir={stage}/ssl")
        toolchain.sh(f"{configure} && make -j{ctx.jobs} && make install_sw", cwd=src, abi=abi, log_name=f"openssl-{abi}")

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
    src = ctx.sources.ensure(C.LIBRARIES["srt"].source, ctx.source_dir("srt"), ctx.force_fetch)
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
        # ffmpeg 找包名 srt，而 srt 安装的是 haisrt.pc
        pc_dir = lib_dir / "pkgconfig"
        if (pc_dir / "haisrt.pc").exists() and not (pc_dir / "srt.pc").exists():
            shutil.copy2(pc_dir / "haisrt.pc", pc_dir / "srt.pc")
    return stage


# ------------------------------------------------------------------ fdk-aac
def build_fdk_aac(ctx, abi: str) -> Path:
    _set_current("fdk-aac")
    src = ctx.sources.ensure(C.LIBRARIES["fdk-aac"].source, ctx.source_dir("fdk-aac"), ctx.force_fetch)
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
    src = ctx.sources.ensure(C.LIBRARIES["openh264"].source, ctx.source_dir("openh264"), ctx.force_fetch)
    toolchain, platform = ctx.toolchain, ctx.platform

    if platform == "ios":
        _hide_symbols(ctx, src)

    if platform == "windows":
        # openh264 的 ARCH 用 x86_64/x86/arm64 命名（不是 VS 的 x64）
        arch = {"x64": "x86_64", "x86": "x86", "arm64": "arm64"}.get(abi, "x86_64")
        script = (f"make -j{ctx.jobs} OS=msvc ARCH={arch} USE_ASM=Yes BUILDTYPE=Release "
                  f"install-static PREFIX={toolchain.msys_path(stage)}")
    elif platform == "android":
        from .toolchains import ANDROID_ABI
        spec = ANDROID_ABI[abi]
        bin_dir = toolchain.toolchain_bin
        script = (f"make -j{ctx.jobs} OS=android NDKROOT={toolchain.ndk} NDKLEVEL={toolchain.api} "
                  f"TARGET=android-{toolchain.api} ARCH={spec['openh264']} NDK_TOOLCHAIN_VERSION=clang "
                  f"GCC_TOOLCHAIN_PATH={bin_dir} SYSROOT={bin_dir / 'sysroot'} "
                  f"TOOLCHAIN_NAME={spec['triple']} TARGET_NAME={spec['triple']}{toolchain.api} "
                  f"install-static PREFIX={stage}")
    else:
        script = (f"make -j{ctx.jobs} OS=ios ARCH={abi.replace('-simulator', '')} "
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
    src = ctx.sources.ensure(C.LIBRARIES["libyuv"].source, ctx.source_dir("libyuv"), ctx.force_fetch)
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
        raise RuntimeError(f"libyuv 静态库未生成: {build_dir}")
    shutil.copy2(built[0], lib_dir / built[0].name)
    if ctx.platform == "android" and not (lib_dir / "libyuv_static.a").exists():
        shutil.copy2(lib_dir / built[0].name, lib_dir / "libyuv_static.a")
    return stage


# ---------------------------------------------------------------------- json
def build_json(ctx, abi: str) -> Path:
    """header-only：只需归集头文件（同时提供 single_include 兼容旧引用路径）。"""
    _set_current("json")
    src = ctx.sources.ensure(C.LIBRARIES["json"].source, ctx.source_dir("json"), ctx.force_fetch)
    stage = ensure_dir(ctx.stage_dir("json", abi))
    source_include = src / "include"
    if not source_include.exists():
        raise RuntimeError(f"json 源码缺少 include: {src}")
    for target_dir in (stage / "include", stage / "single_include"):
        ensure_dir(target_dir)
        for header in source_include.rglob("*.hpp"):
            destination = target_dir / header.relative_to(source_include)
            ensure_dir(destination.parent)
            shutil.copy2(header, destination)
    return stage


# -------------------------------------------------------------------- ffmpeg
def build_ffmpeg(ctx, abi: str) -> Path:
    _set_current("ffmpeg")
    stage = ensure_dir(ctx.stage_dir("ffmpeg", abi))
    lib = C.LIBRARIES["ffmpeg"]

    # 允许 --ffmpeg-ref 覆盖仓库分支/标签
    source = lib.source
    if ctx.ffmpeg_ref and ctx.ffmpeg_ref != source.ref:
        from dataclasses import replace
        from .utils import GitSource
        source = replace(source, ref=ctx.ffmpeg_ref)
    src = ctx.sources.ensure(source, ctx.source_dir("ffmpeg"), ctx.force_fetch)

    prefixes = _dependency_prefixes(ctx, abi)
    args = _ffmpeg_common_args(ctx, abi, stage, prefixes)
    args += _ffmpeg_platform_args(ctx, abi)

    env = ctx.toolchain.build_env(abi)
    separator = getattr(ctx.toolchain, "shell_path_separator", os.path.sep)
    to_path = ctx.toolchain.msys_path if ctx.platform == "windows" else str
    pkg_paths = [to_path(prefix / "lib" / "pkgconfig") for prefix in prefixes]
    if env.get("PKG_CONFIG_PATH"):
        pkg_paths.append(env["PKG_CONFIG_PATH"])
    pkg_config_path = separator.join(pkg_paths)
    ctx.logger.debug(f"PKG_CONFIG_PATH={pkg_config_path}")

    # PKG_CONFIG_PATH 必须在 shell 内 export：外部传入会被 MSYS 做路径转换而失效
    script = (f"export PKG_CONFIG_PATH='{pkg_config_path}' && ./configure "
              + " ".join(args) + f" && make -j{ctx.jobs} && make install")
    ctx.toolchain.sh(script, cwd=src, abi=abi, env=env, log_name=f"ffmpeg-{abi}")

    if ctx.platform == "windows":
        # MSVC 下 import lib 装在 bin/，同步一份到 lib/
        bin_dir, lib_dir = stage / "bin", ensure_dir(stage / "lib")
        for import_lib in bin_dir.glob("*.lib"):
            if not (lib_dir / import_lib.name).exists():
                shutil.copy2(import_lib, lib_dir / import_lib.name)
    return stage


FFMPEG_DEPENDENCIES = ("openssl", "fdk-aac", "openh264", "srt")


def _dependency_prefixes(ctx, abi) -> List[Path]:
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


def _ffmpeg_common_args(ctx, abi, stage: Path, prefixes) -> List[str]:
    encoders = list(C.BASE_ENCODERS) + C.PLATFORM_ENCODERS.get(ctx.platform, [])
    if ctx.enable_nvenc and ctx.platform == "windows":
        encoders.append("h264_nvenc")

    includes = " ".join(f"-I{p}/include" for p in prefixes)
    if ctx.platform == "windows":
        libs = " ".join(f"-LIBPATH:{p}/lib" for p in prefixes)
    else:
        libs = " ".join(f"-L{p}/lib" for p in prefixes)
        includes += " -ffunction-sections -fdata-sections"   # 配合最终链接的 --gc-sections

    to_path = ctx.toolchain.msys_path if ctx.platform == "windows" else str
    return [
        f"--prefix='{to_path(stage)}'",
        "--pkg-config-flags=--static",
        # 组件白名单
        "--disable-everything",
        "--disable-autodetect",
        f"--enable-encoder={','.join(encoders)}",
        f"--enable-decoder={','.join(C.DECODERS)}",
        f"--enable-muxer={','.join(C.MUXERS)}",
        f"--enable-demuxer={','.join(C.DEMUXERS)}",
        f"--enable-parser={','.join(C.PARSERS)}",
        f"--enable-bsf={','.join(C.BITSTREAM_FILTERS)}",
        f"--enable-filter={','.join(C.FILTERS)}",
        f"--enable-protocol={','.join(C.PROTOCOLS)}",
        # 外部库
        "--enable-openssl", "--enable-libfdk-aac", "--enable-libopenh264", "--enable-libsrt",
        "--enable-nonfree", "--enable-version3",
        # 关掉用不到的部分
        "--disable-avdevice", "--disable-postproc", "--disable-vulkan",
        "--disable-programs", "--disable-doc", "--disable-htmlpages",
        "--disable-manpages", "--disable-podpages", "--disable-txtpages",
        "--disable-debug", "--disable-iconv",
        "--enable-small", "--enable-pic",
        f"--extra-cflags='{includes}'",
        f"--extra-ldflags='{libs}'",
    ] + (["--enable-lto"] if ctx.ffmpeg_lto else [])


def _ffmpeg_platform_args(ctx, abi) -> List[str]:
    if ctx.platform == "windows":
        args = ["--target-os=win64", "--arch=x86_64", "--toolchain=msvc",
                "--enable-shared", "--disable-static",
                # openssl 在 Windows 不装 .pc，静态链接所需系统库需显式给出
                "--extra-libs='-lws2_32 -lgdi32 -lcrypt32 -ladvapi32 -luser32'"]
        if ctx.enable_nvenc:
            args.append("--enable-nvenc")
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
    "openssl": build_openssl,
    "srt": build_srt,
    "fdk-aac": build_fdk_aac,
    "openh264": build_openh264,
    "libyuv": build_libyuv,
    "json": build_json,
    "ffmpeg": build_ffmpeg,
}
