"""平台/工具链适配：Windows(MSVC) / Android(NDK) / iOS(Xcode) / OHOS(OpenHarmony)。

新增一个平台：继承 :class:`Toolchain` 并在 :data:`TOOLCHAINS` 中注册。
"""

from __future__ import annotations

import glob
import os
import platform as platform_mod
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .config import PROJECT_ROOT, TOOLS_DIR, is_portable, platform_layout
from .utils import copy_tree, ensure_dir, fix_pkgconfig_prefixes, lipo_create, static_lib_names, to_msys_path, which

VSWHERE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe",
    r"C:\Program Files\Microsoft Visual Studio\Installer\vswhere.exe",
]
VS_GLOB = [
    r"C:\Program Files\Microsoft Visual Studio\*",
    r"C:\Program Files (x86)\Microsoft Visual Studio\*",
]
MSYS2_CANDIDATES = [r"C:\msys64", r"C:\msys32", r"C:\tools\msys64"]


class ToolchainError(RuntimeError):
    """工具链缺失或不可用，``hint`` 给出修复方式。"""

    def __init__(self, tool: str, message: str, hint: str = ""):
        super().__init__(f"[{tool}] {message}" + (f"\n  -> {hint}" if hint else ""))
        self.tool = tool
        self.hint = hint


# ------------------------------------------------------------------ MSVC
class MSVC:
    """一次 ``vcvarsall.bat`` 的环境抓取结果（一个目标架构一份）。"""

    # vcvarsall 一次要几秒；bootstrap 自检 + prepare 会对同一架构反复探测，按参数缓存
    _cache: Dict[str, Optional["MSVC"]] = {}

    def __init__(self, install: Path, vcvarsall: Path, env: Dict[str, str], year: str, arch: str = "x64"):
        self.install = install
        self.vcvarsall = vcvarsall
        self.env = env
        self.year = year
        self.arch = arch

    @property
    def generator(self) -> Optional[str]:
        return {"2022": "Visual Studio 17 2022", "2019": "Visual Studio 16 2019"}.get(self.year)

    @classmethod
    def discover(cls, logger, arch: str = "x64") -> Optional["MSVC"]:
        """``arch`` 是 vcvarsall 的参数：x64 / x86 / arm64，或交叉形式 x64_x86 / x64_arm64。"""
        if arch in cls._cache:
            return cls._cache[arch]
        result = None
        install = cls._find_installation()
        vcvarsall = install / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat" if install else None
        if vcvarsall and vcvarsall.exists():
            env = cls._capture_env(vcvarsall, arch)
            if env and which("cl", env):
                year = next((p for p in install.parts if p in ("2022", "2019", "2017")), "2022")
                result = cls(install, vcvarsall, env, year, arch)
        cls._cache[arch] = result
        return result

    @staticmethod
    def _find_installation() -> Optional[Path]:
        for vswhere in VSWHERE_CANDIDATES:
            if not Path(vswhere).exists():
                continue
            try:
                proc = subprocess.run(
                    [vswhere, "-latest", "-products", "*",
                     "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                     "-property", "installationPath"],
                    capture_output=True, text=True, errors="replace", timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line and Path(line).exists():
                    return Path(line)
        found = [Path(p) for pattern in VS_GLOB for p in glob.glob(pattern)
                 if (Path(p) / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat").exists()]
        return max(found, key=lambda p: p.name) if found else None

    @staticmethod
    def _capture_env(vcvarsall: Path, arch: str) -> Dict[str, str]:
        """必须用 shell 交给 cmd 解析，否则带空格的 .bat 路径无法识别。"""
        command = f'chcp 65001 >nul && call "{vcvarsall}" {arch} >nul && set'
        env: Dict[str, str] = {}
        for encoding in ("utf-8", "mbcs"):
            try:
                proc = subprocess.run(command, shell=True, capture_output=True,
                                      text=True, encoding=encoding, errors="replace", timeout=180)
            except (OSError, subprocess.SubprocessError):
                continue
            env = _normalize_env(_parse_env_block(proc.stdout))
            if which("cl", env):
                return env
        return env


def _parse_env_block(text: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.rstrip("\r\n").partition("=")
        if key.strip() and not key.startswith("!"):
            env[key.strip()] = value
    return env


def _normalize_env(env: Dict[str, str]) -> Dict[str, str]:
    result, path_value = {}, ""
    for key, value in env.items():
        if key.lower() == "path":
            if len(value) > len(path_value):
                path_value = value
        else:
            result[key] = value
    if path_value:
        result["PATH"] = path_value
    return result


# ------------------------------------------------------------------ 基类
class Toolchain:
    name = "base"
    default_abis: List[str] = []
    use_abi_dir: bool = True
    # 通过 POSIX shell 执行时，路径列表环境变量的分隔符
    shell_path_separator: str = os.pathsep
    default_ios_min = "12.0"

    def __init__(self, ctx):
        self.ctx = ctx
        self.logger = ctx.logger
        self.runner = ctx.runner
        # 写入版本文件名（version-<库>-<版本>-g<commit>-<时间>.<平台>.<编译器>.txt）
        self.compiler_id = "unknown"

    # 生命周期
    def prepare(self) -> None:
        """探测工具链，缺失时抛 ToolchainError。"""

    def build_env(self, abi: str) -> Dict[str, str]:
        raise NotImplementedError

    def cmake_args(self, abi: str) -> List[str]:
        raise NotImplementedError

    # 路径 / 目录布局（对齐 MediaX/ESMediaX/3rd 的历史结构）
    def layout_of(self, lib: str) -> str:
        layout = platform_layout(self.name, lib)
        # flat / lib 布局没有 abi 层，同时编多个 ABI 会互相覆盖 → 回落到 abi 目录。
        # 但 fat 库平台（iOS）多架构是 lipo 合并后落在同一目录，本来就不分 abi，不能切。
        if layout != "abi" and len(self.ctx.abis) > 1 and self.use_abi_dir:
            self.logger.info(f"[{lib}] 同时构建 {', '.join(self.ctx.abis)}，改用 abi 目录布局"
                             f"（{layout} 布局无 abi 层会互相覆盖）")
            return "abi"
        return layout

    def install_dir(self, lib: str, abi: Optional[str] = None) -> Path:
        if is_portable(lib):        # header-only：三端共用一份，不带平台/abi 层
            return self.ctx.output_root / lib
        root = self.ctx.output_root / lib / self.name
        if abi and self.layout_of(lib) == "abi":
            return root / abi
        return root

    # 执行
    def shell_command(self, script: str, cwd: Optional[Path], abi: Optional[str] = None):
        """返回 (argv, 子进程 cwd)。

        不能用登录 shell（``-l``）：macOS 的 ``/etc/profile`` 会跑 ``path_helper``，
        把 PATH 按 ``/etc/paths`` 重新排序，系统 ``/usr/bin`` 会被排到我们在
        ``build_env()`` 里手动前置的交叉编译工具链（NDK clang 等）前面——
        表现为 ``which clang`` 命中系统 clang，而不是 NDK 里的那个，进而让
        openssl Configure 之类按路径特征识别工具链的逻辑误判成"NDK 不完整"。
        普通 ``-c`` 不会触发 path_helper，PATH 完全听 build_env() 的安排。
        """
        return ["/bin/bash", "-c", script], cwd

    def sh(self, script: str, cwd: Path, abi: str, env: Optional[Dict[str, str]] = None, **kw):
        argv, effective_cwd = self.shell_command(script, cwd, abi)
        return self.runner.run(argv, cwd=effective_cwd, env=env or self.build_env(abi), **kw)

    def run(self, cmd, cwd: Path, abi: str, env: Optional[Dict[str, str]] = None, **kw):
        return self.runner.run(list(cmd), cwd=cwd, env=env or self.build_env(abi), **kw)

    # ABI 解析（各平台共用一套规则，只差下面三张表）
    #: 本平台全部合法 ABI（顺序即 ``all`` 展开后的构建顺序）
    supported_abis: List[str] = []
    #: ``all`` / ``*`` 展开成哪些；None 表示等于 supported_abis
    all_abis: Optional[List[str]] = None
    #: 别名 -> 若干 ABI（如 iOS 的 device / simulator / fat）
    abi_aliases: Dict[str, List[str]] = {}

    def expand_abis(self, requested: Optional[List[str]]) -> List[str]:
        """``--abi`` 统一解析：空格或逗号分隔都行，支持 ``all`` 与平台别名，去重保序。

        不认识的值**直接报错**：以前 Windows 会把非法值悄悄丢掉回落成 x64，
        ``--abi x64,x86`` 这种写法也被整体当成非法值吞掉，结果只编了 x64 却没有任何提示。
        """
        items = [part.strip() for raw in (requested or []) for part in str(raw).split(",") if part.strip()]
        if not items:
            return list(self.default_abis)
        expanded: List[str] = []
        unknown: List[str] = []
        for item in items:
            key = item.lower()
            if key in ("all", "*"):
                expanded += list(self.all_abis if self.all_abis is not None else self.supported_abis)
            elif key in self.abi_aliases:
                expanded += self.abi_aliases[key]
            elif item in self.supported_abis:
                expanded.append(item)
            else:
                unknown.append(item)
        if unknown:
            choices = list(self.supported_abis) + list(self.abi_aliases) + ["all"]
            raise ToolchainError(f"{self.name} ABI", f"不支持: {', '.join(unknown)}",
                                 f"可选: {', '.join(choices)}（多个值用空格或逗号分隔）")
        return list(dict.fromkeys(expanded))

    # 产物

    def finalize(self, lib: str, stages: Dict[str, Path]) -> Path:
        """按布局落盘：flat 直放、lib 把 lib/* 收到 lib/<abi>/、abi 放到 <平台>/<abi>。"""
        portable = is_portable(lib)
        target = self.install_dir(lib, next(iter(stages)))
        for abi, stage in stages.items():
            destination = self.install_dir(lib, abi) if not portable else target
            ensure_dir(destination)
            if portable:
                # header-only 的目录名不固定（include / single_include / …），整份拷贝，别按名单挑；
                # 拷贝前先清掉同名旧内容，否则换 output_subdirs 时新旧头文件会混在一起
                for item in stage.iterdir():
                    stale = destination / item.name
                    if stale.is_dir():
                        shutil.rmtree(stale, ignore_errors=True)
                    elif stale.exists():
                        stale.unlink()
                copy_tree(stage, destination)
                continue
            for sub in ("include", "lib", "bin", "share"):
                if (stage / sub).exists():
                    copy_tree(stage / sub, destination / sub)
            if self.layout_of(lib) == "lib":      # spdlog：abi 层放在 lib 里面
                self._move_lib_into_abi(destination, abi)
            fix_pkgconfig_prefixes(destination)
        return target

    @staticmethod
    def _move_lib_into_abi(target: Path, abi: str) -> None:
        """把 <target>/lib/* 挪到 <target>/lib/<abi>/*（幂等，重跑不会嵌套）。"""
        lib_dir = target / "lib"
        if not lib_dir.exists():
            return
        dest = lib_dir / abi
        if dest.exists():
            return
        dest.mkdir(parents=True, exist_ok=True)
        for entry in list(lib_dir.iterdir()):
            if entry == dest:
                continue
            shutil.move(str(entry), str(dest / entry.name))


# ------------------------------------------------------------------ Windows
# 每个 ABI 一整套参数：vcvarsall 的目标架构、CMake VS 生成器的 -A、openssl 的 Configure target、
# ffmpeg 的 --arch/--target-os、openh264 的 ARCH。以前这几处都写死成 x64，
# --abi x86/arm64 编出来的仍是 x64 二进制，只是被放进了 x86/arm64 目录。
WINDOWS_ABI = {
    "x64": {"target": "x64", "cmake_platform": "x64", "openssl": "VC-WIN64A",
            "ffmpeg_arch": "x86_64", "ffmpeg_os": "win64", "openh264": "x86_64", "nasm": True},
    "x86": {"target": "x86", "cmake_platform": "Win32", "openssl": "VC-WIN32",
            "ffmpeg_arch": "x86", "ffmpeg_os": "win32", "openh264": "x86", "nasm": True},
    "arm64": {"target": "arm64", "cmake_platform": "ARM64", "openssl": "VC-WIN64-ARM",
              "ffmpeg_arch": "aarch64", "ffmpeg_os": "win64", "openh264": "arm64", "nasm": False},
}
# platform.machine() -> vcvarsall 的宿主架构名
WINDOWS_HOST_ARCH = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64",
                     "x86": "x86", "i386": "x86", "i686": "x86"}


def windows_host_arch() -> str:
    return WINDOWS_HOST_ARCH.get(platform_mod.machine().lower(), "x64")


def vcvars_arch(abi: str, host: Optional[str] = None) -> str:
    """vcvarsall 的参数：宿主与目标相同写 ``x64``，不同写交叉形式 ``x64_arm64``。"""
    host = host or windows_host_arch()
    target = WINDOWS_ABI[abi]["target"]
    return target if host == target else f"{host}_{target}"


def msvc_targets(msvc: Optional[MSVC], abi: str) -> bool:
    """vcvarsall 找不到对应架构的组件时有可能"成功返回"但仍给出别的 cl，按 cl 所在目录核对。

    cl 位于 ``...\\bin\\Host<宿主>\\<目标>\\cl.exe``，目标目录名与 WINDOWS_ABI 的 target 一致。
    """
    if not msvc:
        return False
    cl = which("cl", msvc.env)
    return bool(cl) and Path(cl).parent.name.lower() == WINDOWS_ABI[abi]["target"]


def gas_preprocessor(env: Optional[Dict[str, str]] = None) -> str:
    """MSVC 下编 ARM64 汇编（ffmpeg / openh264）需要 gas-preprocessor.pl + armasm64。"""
    return which("gas-preprocessor.pl", env) or ""


class WindowsToolchain(Toolchain):
    """优先微软编译套件：MSVC + MSYS2(GNU make/install/perl/nasm)。"""

    name = "windows"
    default_abis = ["x64"]
    supported_abis = list(WINDOWS_ABI)
    use_abi_dir = True
    shell_path_separator = ":"

    def __init__(self, ctx, prefer: str = "auto"):
        super().__init__(ctx)
        self.prefer = prefer
        self.crt = (getattr(ctx, "crt", "mt") or "mt").lower()
        self.compiler_id = "msvc"          # prepare() 中按实际探测结果修正
        self.msvc: Optional[MSVC] = None   # 第一个 ABI 的环境（兼容旧调用方）
        self.msvc_by_abi: Dict[str, MSVC] = {}
        self.msys2_root: Optional[Path] = None
        self.msys2_bins: List[str] = []
        self.ninja = self.cmake = None

    def prepare(self) -> None:
        self._detect_msys2()
        if self.prefer in ("auto", "msvc"):
            missing = []
            for abi in self.ctx.abis:
                msvc = MSVC.discover(self.logger, vcvars_arch(abi))
                if msvc_targets(msvc, abi):
                    self.msvc_by_abi[abi] = msvc
                else:
                    missing.append(abi)
            if missing and self.msvc_by_abi:
                # 有 MSVC，但缺某个目标架构的编译器组件：直接报错，绝不拿别的架构顶替
                raise ToolchainError(
                    "MSVC", f"VS 未安装 {', '.join(missing)} 目标的编译器（vcvarsall "
                    f"{', '.join(vcvars_arch(a) for a in missing)} 失败）",
                    "VS Installer → 单个组件：x86 选 'MSVC v143 - VS 2022 C++ x64/x86 生成工具'，"
                    "arm64 选 'MSVC v143 - VS 2022 C++ ARM64/ARM64EC 生成工具'")
            self.msvc = self.msvc_by_abi.get(self.ctx.abis[0])
        if self.msvc:
            self.compiler_id = "msvc"
            for abi, msvc in self.msvc_by_abi.items():
                self.logger.info(f"MSVC[{abi}]: {msvc.install} (vcvarsall {msvc.arch}, cl={which('cl', msvc.env)})")
        else:
            self.compiler_id = "mingw"
        if not self.msvc and self.prefer == "msvc":
            raise ToolchainError("MSVC", "未检测到 VS / VS Build Tools",
                                 "python build.py bootstrap --platform windows --install")
        elif not self.msvc and not (which("gcc") or (self.msys2_root and (self.msys2_root / "mingw64" / "bin" / "gcc.exe").exists())):
            raise ToolchainError("MSVC/MinGW", "既无 MSVC 也无 MinGW",
                                 "安装 VS Build Tools 或 MSYS2")

        env = self.build_env(self.ctx.abis[0])
        self.cmake = which("cmake", env)
        self.ninja = which("ninja", env)
        if not self.cmake:
            raise ToolchainError("cmake", "未找到 cmake", "winget install Kitware.CMake")
        if not which("make", env):
            raise ToolchainError("make", "openh264 的 Makefile 需要 GNU make",
                                 "MSYS2: pacman -S make，或 winget install MSYS2.MSYS2")
        if "arm64" in self.ctx.abis and not gas_preprocessor(env):
            self.logger.warn("未找到 gas-preprocessor.pl：arm64 的 ffmpeg 将 --disable-asm、openh264 将 USE_ASM=No"
                             "（能编出可用产物，但没有 NEON 汇编优化）")
        self.logger.info(f"工具链就绪: cmake={self.cmake} ninja={self.ninja or '-'}")

    def _detect_msys2(self) -> None:
        for candidate in MSYS2_CANDIDATES:
            root = Path(candidate)
            if (root / "usr" / "bin" / "bash.exe").exists():
                self.msys2_root = root
                self.msys2_bins = [str(root / "usr" / "bin")]
                if (root / "mingw64" / "bin").exists():
                    self.msys2_bins.append(str(root / "mingw64" / "bin"))
                return

    def msvc_for(self, abi: Optional[str]) -> Optional[MSVC]:
        """该 ABI 对应的 vcvarsall 环境（x86 / arm64 的 cl、LIB、INCLUDE 都与 x64 不同）。"""
        return self.msvc_by_abi.get(abi or "") or self.msvc

    def build_env(self, abi: str = "x64") -> Dict[str, str]:
        msvc = self.msvc_for(abi)
        base = dict(msvc.env) if msvc else os.environ.copy()
        # cl 的诊断信息会跟随系统语言：中文语言包下 -showIncludes 输出的是
        # "注意: 包含文件:"，而 ffmpeg 的 ffbuild/common.mak 只按英文
        # "Note: including file:" 做 sed 提取 —— 结果所有 C/C++ 的 .d 依赖文件都是
        # 0 字节，make 感知不到 config.h 变化（曾出现"配置改了却只重编一部分"：
        # DLL 有硬编、ffmpeg.exe 里的 configuration 串却是旧的）。
        # VSLANG=1033 强制英文资源，依赖追踪即恢复正常。
        # 实测：只装了中文语言包时 VSLANG 无效（cl 依旧输出"注意: 包含文件:"），
        # 此时 .d 仍是空文件 —— 由 recipes._ensure_build_dir() 的 configure 指纹兜底，
        # 保证"配置变了就全量重编"，不会出现配置与产物不一致。
        base.setdefault("VSLANG", "1033")
        if self.msys2_bins:
            # MSVC 路径必须在前，MSYS2 只作为 GNU 工具补充（否则 cmake 会选到
            # /usr/bin/c++.exe，GNU link.exe 也会抢在 MSVC link.exe 之前）
            current = [p for p in base.get("PATH", "").split(os.pathsep) if p]
            for entry in self.msys2_bins:
                if entry not in current:
                    current.append(entry)
            base["PATH"] = os.pathsep.join(current)
        return base

    def cmake_args(self, abi: str = "x64") -> List[str]:
        spec = WINDOWS_ABI.get(abi, WINDOWS_ABI["x64"])
        msvc = self.msvc_for(abi)
        # Ninja：目标架构由该 ABI 的 vcvarsall 环境（cl 本身）决定；
        # VS 生成器：不吃 vcvars 环境，必须用 -A 指定平台
        args = ["-G", "Ninja"] if self.ninja else []
        if not self.ninja and msvc and msvc.generator:
            args = ["-G", msvc.generator, "-A", spec["cmake_platform"]]
        if self.ninja and spec["target"] != windows_host_arch() and spec["target"] == "arm64":
            # 交叉到 ARM64：CMake 默认把 CMAKE_SYSTEM_PROCESSOR 设成宿主 AMD64，
            # libyuv 等按它决定是否编 NEON 内核 —— 必须显式告诉 CMake 目标处理器
            args += ["-DCMAKE_SYSTEM_NAME=Windows", "-DCMAKE_SYSTEM_PROCESSOR=ARM64"]
        args += ["-DCMAKE_BUILD_TYPE=Release"]
        if msvc:
            # 默认 /MT，与 openssl/ffmpeg 一致（否则 ffmpeg 链接时 CRT 打架）；
            # --crt md 给 /MD 的消费方（如 ESMediaX），典型只用于应用直链的库（spdlog/libyuv/json）
            args += [
                f"-DCMAKE_MSVC_RUNTIME_LIBRARY={self.crt_lib}",
                f"-DCMAKE_C_FLAGS_RELEASE={self.crt_flag} /O2 /Ob2 /DNDEBUG",
                f"-DCMAKE_CXX_FLAGS_RELEASE={self.crt_flag} /O2 /Ob2 /DNDEBUG",
            ]
        return args

    @property
    def crt_flag(self) -> str:
        """/MD（动态 CRT）或 /MT（静态 CRT）。"""
        return "/MD" if self.crt == "md" else "/MT"

    @property
    def marker_variant(self) -> str:
        """记进版本文件名的重要信息：Windows 的 CRT（mt/md）。"""
        return f"crt{self.crt}"

    @property
    def crt_lib(self) -> str:
        return "MultiThreadedDLL" if self.crt == "md" else "MultiThreaded"

    @property
    def needs_config_type(self) -> bool:
        return not bool(self.ninja)

    def shell_command(self, script: str, cwd: Optional[Path], abi: Optional[str] = None):
        """MSYS2 登录 shell 不继承 Windows cwd，且会重排 PATH。

        因此脚本内必须做两件事：cd 到工作目录（/d/... 形式），并把**该 ABI 的** MSVC
        路径以 POSIX 形式（/c/...）置顶，否则 make 找不到 cl.exe（或找到别的架构的 cl）。
        """
        prefix = self._msvc_path_prefix(abi)
        if prefix:
            script = f'export PATH="{prefix}:$PATH" && {script}'
        if cwd:
            script = f'cd "{to_msys_path(cwd)}" && {script}'
        return [self._bash(), "-lc", script], None

    def _msvc_path_prefix(self, abi: Optional[str] = None) -> str:
        msvc = self.msvc_for(abi)
        if not msvc:
            return ""
        base = {p.rstrip("\\/").lower() for p in os.environ.get("PATH", "").split(os.pathsep)}
        entries = [
            to_msys_path(entry)
            for entry in msvc.env.get("PATH", "").split(os.pathsep)
            if entry and entry.rstrip("\\/").lower() not in base
        ]
        if self.msys2_root:
            entries.append(str(self.msys2_root / "usr" / "bin").replace("\\", "/"))
        return ":".join(entries)

    def _bash(self) -> str:
        if self.msys2_root:
            return str(self.msys2_root / "usr" / "bin" / "bash.exe")
        fallback = which("bash")
        if fallback:
            return fallback
        raise ToolchainError("MSYS2", "未找到 MSYS2 bash", "安装 MSYS2 后 pacman -S make perl nasm")

    def msys_path(self, path) -> str:
        return to_msys_path(path)


# ------------------------------------------------------------------ Android
ANDROID_ABI = {
    "arm64-v8a": {"arch": "arm64", "triple": "aarch64-linux-android", "openssl": "android-arm64", "openh264": "arm64"},
    "armeabi-v7a": {"arch": "arm", "triple": "armv7a-linux-androideabi", "openssl": "android-arm", "openh264": "arm"},
    "x86_64": {"arch": "x86_64", "triple": "x86_64-linux-android", "openssl": "android-x86_64", "openh264": "x86_64"},
}
NDK_HOST = {"Darwin": "darwin-x86_64", "Windows": "windows-x86_64", "Linux": "linux-x86_64"}


class AndroidToolchain(Toolchain):
    name = "android"
    default_abis = ["arm64-v8a"]
    supported_abis = list(ANDROID_ABI)
    # all 只展开真机 ABI；x86_64（模拟器）要显式写
    all_abis = ["arm64-v8a", "armeabi-v7a"]

    def __init__(self, ctx, ndk: Optional[str] = None, api: int = 21):
        super().__init__(ctx)
        self.ndk: Optional[Path] = None
        self.ndk_hint = ndk
        self.compiler_id = "ndk-clang"
        self.api = int(api or 21)
        self.host_tag = NDK_HOST.get(platform_mod.system(), "linux-x86_64")

    def prepare(self) -> None:
        self.ndk = self._detect_ndk()
        if not self.ndk:
            raise ToolchainError("Android NDK", "未找到 NDK",
                                 "设置 ANDROID_NDK_HOME 或 python build.py bootstrap --platform android --install")
        if not (self.ndk / "toolchains" / "llvm" / "prebuilt" / self.host_tag).exists():
            raise ToolchainError("Android NDK", f"缺少 {self.host_tag} 工具链", "检查 NDK 版本")
        unknown = [a for a in self.ctx.abis if a not in ANDROID_ABI]
        if unknown:
            raise ToolchainError("Android ABI", f"不支持: {unknown}", f"可选: {', '.join(ANDROID_ABI)}")
        self.logger.info(f"NDK: {self.ndk} (host={self.host_tag}, api={self.api})")

    @property
    def marker_variant(self) -> str:
        """记进版本文件名的重要信息：Android API 级别（+ NDK 版本）。"""
        ndk = getattr(self, "ndk_version", "") or ""
        return f"api{self.api}" + (f"-ndk{ndk}" if ndk else "")

    def _detect_ndk(self) -> Optional[Path]:
        candidates: List[Path] = []
        if self.ndk_hint:
            candidates.append(Path(self.ndk_hint).expanduser())
        for var in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK", "NDK21", "NDK20"):
            if os.environ.get(var):
                candidates.append(Path(os.environ[var]).expanduser())
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            root = os.environ.get(var)
            if root:
                candidates += sorted(Path(root).expanduser().glob("ndk/*"), reverse=True)
        home = Path.home()
        for sdk in (home / "Library/Android/sdk", home / "Android/Sdk"):
            if sdk.exists():
                candidates += sorted(sdk.glob("ndk/*"), reverse=True)
        candidates += sorted((PROJECT_ROOT / TOOLS_DIR).glob("android-ndk*"), reverse=True)
        for candidate in candidates:
            if (candidate / "ndk-build").exists():
                return candidate.resolve()
        return None

    @property
    def toolchain_bin(self) -> Path:
        return self.ndk / "toolchains" / "llvm" / "prebuilt" / self.host_tag / "bin"

    @property
    def sysroot(self) -> Path:
        # sysroot 与 bin 是 llvm/prebuilt/<host>/ 下的平级目录（不是 bin/sysroot）；
        # 写错会导致链接期找不到 crtbegin_dynamic.o 等启动对象，报 "cannot open" 致命错误
        return self.toolchain_bin.parent / "sysroot"

    def build_env(self, abi: str) -> Dict[str, str]:
        spec = ANDROID_ABI[abi]
        bin_dir, sysroot = self.toolchain_bin, self.sysroot
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
        env.update({
            "ANDROID_NDK_ROOT": str(self.ndk), "ANDROID_NDK": str(self.ndk),
            "ANDROID_API": str(self.api), "SYSROOT": str(sysroot),
            "CC": str(bin_dir / f"{spec['triple']}{self.api}-clang"),
            "CXX": str(bin_dir / f"{spec['triple']}{self.api}-clang++"),
            "AR": str(bin_dir / "llvm-ar"), "RANLIB": str(bin_dir / "llvm-ranlib"),
            "STRIP": str(bin_dir / "llvm-strip"), "NM": str(bin_dir / "llvm-nm"),
            "CFLAGS": "-fPIC -O3", "CXXFLAGS": "-fPIC -O3",
        })
        return env

    def cmake_args(self, abi: str) -> List[str]:
        return [
            "-G", "Unix Makefiles",
            f"-DCMAKE_TOOLCHAIN_FILE={self.ndk / 'build' / 'cmake' / 'android.toolchain.cmake'}",
            f"-DANDROID_ABI={abi}", f"-DANDROID_PLATFORM=android-{self.api}",
            "-DANDROID_STL=c++_static", "-DANDROID_ARM_NEON=ON", "-DCMAKE_BUILD_TYPE=Release",
        ]


# ------------------------------------------------------------------ iOS
class IOSToolchain(Toolchain):
    name = "ios"
    default_abis = ["arm64", "x86_64"]
    # openssl 有对应 target 的 ABI（见 recipes.IOS_OPENSSL_TARGETS）
    supported_abis = ["arm64", "x86_64", "arm64-simulator"]
    all_abis = ["arm64", "x86_64"]
    abi_aliases = {"fat": ["arm64", "x86_64"], "device": ["arm64"], "simulator": ["x86_64"]}
    use_abi_dir = False          # 产物为 fat 库

    def __init__(self, ctx, min_version: str = "12.0"):
        super().__init__(ctx)
        self.min_version = min_version or self.default_ios_min
        self.compiler_id = "xcode-clang"
        self._sdk: Dict[str, str] = {}

    @property
    def marker_variant(self) -> str:
        """记进版本文件名的重要信息：iOS 最低系统版本（3rd-bak 的文件名里也是这个）。"""
        return f"min{self.min_version}"

    def prepare(self) -> None:
        if platform_mod.system() != "Darwin":
            raise ToolchainError("Xcode", "iOS 只能在 macOS 上编译", "换一台装了 Xcode 的 Mac")
        if not self._xcrun(["xcode-select", "-p"]):
            raise ToolchainError("Xcode", "未找到 Xcode 命令行工具", "xcode-select --install")
        for abi in self.ctx.abis:
            if not self.sdk_path(abi):
                raise ToolchainError("iOS SDK", f"缺少 {self.sdk_name(abi)}", "安装完整版 Xcode")
        if not which("cmake"):
            raise ToolchainError("cmake", "未找到 cmake", "brew install cmake")
        if "x86_64" in self.ctx.abis and not which("nasm"):
            raise ToolchainError("nasm", "x86_64 的 openh264 需要 nasm", "brew install nasm")
        self.logger.info(f"Xcode 就绪，SDK: {[self.sdk_name(a) for a in self.ctx.abis]}")

    @staticmethod
    def sdk_name(abi: str) -> str:
        return "iphonesimulator" if abi in ("x86_64", "i386", "arm64-simulator") else "iphoneos"

    def sdk_path(self, abi: str) -> str:
        if abi not in self._sdk:
            self._sdk[abi] = self._xcrun(["xcrun", "--sdk", self.sdk_name(abi), "--show-sdk-path"])
        return self._sdk[abi]

    def build_env(self, abi: str) -> Dict[str, str]:
        sim = self.sdk_name(abi) == "iphonesimulator"
        flag = f"-mios-simulator-version-min={self.min_version}" if sim else f"-miphoneos-version-min={self.min_version}"
        env = os.environ.copy()
        env.update({"SDKROOT": self.sdk_path(abi), "IOS_MIN_VERSION": self.min_version,
                    "CFLAGS": flag, "CXXFLAGS": flag, "LDFLAGS": flag})
        return env

    def cmake_args(self, abi: str) -> List[str]:
        # iOS 交叉编译时 CMake 不会自动设 CMAKE_SYSTEM_PROCESSOR；而 libyuv 正是靠它决定
        # 要不要编 row_neon64 / row_sve 等内核目标，不设则库里缺这些符号（链接报未定义）
        return ["-G", "Unix Makefiles", "-DCMAKE_SYSTEM_NAME=iOS",
                f"-DCMAKE_SYSTEM_PROCESSOR={abi.replace('-simulator', '')}",
                f"-DCMAKE_OSX_SYSROOT={self.sdk_name(abi)}",
                f"-DCMAKE_OSX_ARCHITECTURES={abi.replace('-simulator', '')}",
                f"-DCMAKE_OSX_DEPLOYMENT_TARGET={self.min_version}", "-DCMAKE_BUILD_TYPE=Release"]

    def finalize(self, lib: str, stages: Dict[str, Path]) -> Path:
        """多架构 -> lipo 合并为 fat 库。"""
        target = ensure_dir(self.install_dir(lib))
        first = next(iter(stages.values()))
        copy_tree(first / "include", target / "include")
        lib_dir = ensure_dir(target / "lib")
        for name in static_lib_names(first / "lib"):
            inputs = [stage / "lib" / name for stage in stages.values()]
            if len(stages) > 1:
                lipo_create(self.runner, lib_dir / name, inputs)
            else:
                import shutil
                shutil.copy2(inputs[0], lib_dir / name)
        # 静态库之外还有 cmake/pkgconfig 等元数据，从第一个架构直接带出
        for extra in ("cmake", "pkgconfig"):
            source_dir = first / "lib" / extra
            if source_dir.exists():
                copy_tree(source_dir, lib_dir / extra)
        fix_pkgconfig_prefixes(target)
        self.logger.info(f"[{lib}] fat 库 -> {target} ({', '.join(stages)})")
        return target

    def _xcrun(self, cmd: List[str]) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""


# ------------------------------------------------------------------ OHOS
# OpenHarmony：使用 DevEco / commandline-tools 自带的 LLVM + CMake 交叉编译。
# 环境变量与 OHOS 官方 ohos.toolchain.cmake 约定一致。
OHOS_ABI = {
    "arm64-v8a": {"arch": "aarch64", "triple": "aarch64-unknown-linux-ohos",
                  "openssl": "linux-aarch64", "openh264": "arm64", "march": "", "neon": True},
    "armeabi-v7a": {"arch": "arm", "triple": "armv7-unknown-linux-ohos",
                    "openssl": "linux-armv4", "openh264": "arm", "march": "-march=armv7a", "neon": False},
    "x86_64": {"arch": "x86_64", "triple": "x86_64-unknown-linux-ohos",
               "openssl": "linux-x86_64", "openh264": "x86_64", "march": "", "neon": False},
}
OHOS_SDK_ENV_VARS = ("OHOS_SDK", "OHOS_SDK_HOME", "OHOS_NDK_HOME", "HOS_SDK_HOME", "DEVECO_SDK_HOME")
OHOS_SDK_GLOBS = (
    "~/Library/Huawei/Sdk/*/openharmony",             # macOS DevEco Studio
    "~/AppData/Local/Huawei/Sdk/*/openharmony",       # Windows DevEco Studio
    "/Applications/DevEco Studio.app/Contents/sdk/*/openharmony",
    "C:/Program Files/Huawei/DevEco Studio/sdk/*/openharmony",
)


class OHOSToolchain(Toolchain):
    name = "ohos"
    default_abis = ["arm64-v8a"]
    supported_abis = list(OHOS_ABI)

    def __init__(self, ctx, sdk: Optional[str] = None):
        super().__init__(ctx)
        self.sdk_hint = sdk
        self.compiler_id = "ohos-clang"
        self.sdk: Optional[Path] = None
        self.cmake_bin = ""
        self.ninja = ""

    # 生命周期
    def prepare(self) -> None:
        self.sdk = self.detect_sdk()
        if not self.sdk:
            raise ToolchainError(
                "OHOS SDK", "未找到 OpenHarmony SDK",
                "设置 OHOS_SDK=<.../sdk/default/openharmony>，或 python build.py bootstrap --platform ohos")
        missing_dirs = [p for p in (self.llvm_bin, self.sysroot) if not p.exists()]
        if missing_dirs:
            raise ToolchainError("OHOS SDK", f"SDK 不完整，缺少: {', '.join(str(p) for p in missing_dirs)}",
                                 "确认 OHOS_SDK 指向含 native/{llvm,sysroot} 的目录")
        unknown = [a for a in self.ctx.abis if a not in OHOS_ABI]
        if unknown:
            raise ToolchainError("OHOS ABI", f"不支持: {unknown}", f"可选: {', '.join(OHOS_ABI)}")
        # openssl/openh264/ffmpeg 都通过 bash + GNU make 构建，perl 用于 openssl Configure
        if not self._find_bash():
            raise ToolchainError("bash", "OHOS 交叉编译需要 bash（Linux/macOS 原生，或 Windows 上的 MSYS2/Git Bash）",
                                 "在 WSL 或 Linux/macOS 上执行，或安装 MSYS2")
        env = self.build_env(self.ctx.abis[0])
        self.cmake_bin = self._sdk_tool("cmake") or which("cmake", env) or ""
        if not self.cmake_bin:
            raise ToolchainError("cmake", "未找到 cmake", "apt install cmake / brew install cmake")
        self.ninja = self._sdk_tool("ninja") or which("ninja", env) or ""
        if not which("make", env):
            raise ToolchainError("make", "openssl/openh264/ffmpeg 需要 GNU make", "apt install make / brew install make")
        if not which("perl", env):
            raise ToolchainError("perl", "openssl 的 Configure 需要 perl", "apt install perl / brew install perl")
        self.logger.info(f"OHOS SDK: {self.sdk} (cmake={self.cmake_bin}, ninja={self.ninja or '-'})")
        if not self.toolchain_file.exists():
            self.logger.warn(f"SDK 缺少 {self.toolchain_file}，CMake 交叉编译可能失败")

    def detect_sdk(self) -> Optional[Path]:
        candidates: List[Path] = []
        if self.sdk_hint:
            candidates.append(Path(self.sdk_hint).expanduser())
        for var in OHOS_SDK_ENV_VARS:
            if os.environ.get(var):
                candidates.append(Path(os.environ[var]).expanduser())
        for pattern in OHOS_SDK_GLOBS:
            candidates += sorted(Path(pattern).expanduser().parent.glob(Path(pattern).name), reverse=True)
        candidates += sorted((PROJECT_ROOT / TOOLS_DIR).glob("ohos-sdk*"), reverse=True)
        for candidate in candidates:
            resolved = self._as_sdk_root(candidate)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _as_sdk_root(path: Path) -> Optional[Path]:
        """兼容两种写法：.../openharmony 与 .../openharmony/native。"""
        try:
            if (path / "native" / "llvm").exists():
                return path.resolve()
            if path.name == "native" and (path / "llvm").exists():
                return path.parent.resolve()
        except OSError:
            return None
        return None

    # 路径
    @property
    def llvm_bin(self) -> Path:
        return self.sdk / "native" / "llvm" / "bin"

    @property
    def sysroot(self) -> Path:
        return self.sdk / "native" / "sysroot"

    @property
    def build_tools_bin(self) -> Path:
        return self.sdk / "native" / "build-tools" / "cmake" / "bin"

    @property
    def toolchain_file(self) -> Path:
        return self.sdk / "native" / "build" / "cmake" / "ohos.toolchain.cmake"

    def _sdk_tool(self, name: str) -> str:
        candidate = self.build_tools_bin / name
        return str(candidate) if candidate.exists() else ""

    def build_env(self, abi: str) -> Dict[str, str]:
        spec = OHOS_ABI[abi]
        llvm, sysroot = self.llvm_bin, self.sysroot
        env = os.environ.copy()
        extra = [str(llvm)]
        if self.build_tools_bin.exists():
            extra.append(str(self.build_tools_bin))
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        march = spec["march"]
        cflags = " ".join(x for x in ("-DOHOS_NDK", "-fPIC", march, "-D__MUSL__=1") if x)
        env.update({
            "OHOS_SDK": str(self.sdk), "OHOS_ARCH": abi, "SYSROOT": str(sysroot),
            "AS": str(llvm / "llvm-as"),
            "CC": str(llvm / f"{spec['triple']}-clang"),
            "CXX": str(llvm / f"{spec['triple']}-clang++"),
            "LD": str(llvm / "ld.lld"),
            "AR": str(llvm / "llvm-ar"), "RANLIB": str(llvm / "llvm-ranlib"),
            "STRIP": str(llvm / "llvm-strip"), "NM": str(llvm / "llvm-nm"),
            "OBJDUMP": str(llvm / "llvm-objdump"), "OBJCOPY": str(llvm / "llvm-objcopy"),
            "CFLAGS": cflags, "CXXFLAGS": cflags, "LDFLAGS": "",
        })
        return env

    def cmake_args(self, abi: str) -> List[str]:
        return [
            "-G", "Ninja" if self.ninja else "Unix Makefiles",
            f"-DCMAKE_TOOLCHAIN_FILE={self.toolchain_file}",
            "-DOHOS_PLATFORM=OHOS", f"-DOHOS_ARCH={abi}",
            "-DCMAKE_BUILD_TYPE=Release",
        ]

    # 执行：Linux/macOS 用系统 bash；Windows 用 MSYS2/Git Bash
    def _find_bash(self) -> str:
        if platform_mod.system() != "Windows":
            return "/bin/bash"
        for candidate in MSYS2_CANDIDATES:
            bash = Path(candidate) / "usr" / "bin" / "bash.exe"
            if bash.exists():
                return str(bash)
        return which("bash") or ""

    def shell_command(self, script: str, cwd: Optional[Path], abi: Optional[str] = None):
        # 同 Toolchain.shell_command：避免登录 shell 触发 macOS path_helper 打乱 PATH 顺序
        return [self._find_bash() or "/bin/bash", "-c", script], cwd


# ------------------------------------------------------------------ Linux
# 本机 Linux 构建（gcc/clang + GNU make）：用于服务端开播/推流，也用来在本机
# 快速验证裁剪白名单是否真的编进产物（产物能直接跑，自检不必等到 Windows）。
LINUX_ABI = {
    "x86_64": {"arch": "x86_64", "openssl": "linux-x86_64", "openh264": "x86_64"},
    "arm64": {"arch": "aarch64", "openssl": "linux-aarch64", "openh264": "arm64"},
    "armhf": {"arch": "arm", "openssl": "linux-armv4", "openh264": "arm"},
}
# uname -m -> 本工程使用的 ABI 名（默认 ABI 取构建主机实际架构，不写死 x86_64）
LINUX_HOST_ARCH = {"x86_64": "x86_64", "amd64": "x86_64",
                   "aarch64": "arm64", "arm64": "arm64",
                   "armv7l": "armhf", "armv8l": "armhf"}


class LinuxToolchain(Toolchain):
    name = "linux"
    default_abis = [LINUX_HOST_ARCH.get(platform_mod.machine(), "x86_64")]
    supported_abis = list(LINUX_ABI)
    use_abi_dir = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cc = which("gcc") or which("clang") or which("cc") or ""
        self.cxx = which("g++") or which("clang++") or which("c++") or ""
        self.compiler_id = "clang" if "clang" in Path(self.cc).name else "gcc"
        # x86 汇编（ffmpeg 的 x86asm / openh264 的 asm）需要 nasm；缺了不是致命错误，
        # 由配方降级为 --disable-x86asm / USE_ASM=No，而不是编译到一半才炸
        self.nasm = which("nasm") or which("yasm") or ""

    def prepare(self) -> None:
        if platform_mod.system() != "Linux":
            raise ToolchainError("Linux", f"Linux 产物需在 Linux 上构建（当前宿主 {platform_mod.system()}）",
                                 "换一台 Linux 机器，或改用 --platform windows/android/ios/ohos")
        if not self.cc:
            raise ToolchainError("gcc/clang", "未找到 C 编译器", "dnf install -y gcc / apt install -y build-essential")
        for tool, hint in (("make", "openssl/openh264/ffmpeg 需要 GNU make"),
                           ("cmake", "srt/fdk-aac/zlib/libyuv 用 CMake 构建"),
                           ("pkg-config", "ffmpeg 的 configure 靠它找 openssl/srt/fdk-aac/openh264"),
                           ("perl", "openssl 的 Configure 需要 perl")):
            if not which(tool):
                raise ToolchainError(tool, f"未找到 {tool}（{hint}）", f"dnf install -y {tool} / apt install -y {tool}")
        unknown = [a for a in self.ctx.abis if a not in LINUX_ABI]
        if unknown:
            raise ToolchainError("Linux ABI", f"不支持: {unknown}", f"可选: {', '.join(LINUX_ABI)}")
        if not self.nasm:
            self.logger.warn("未找到 nasm/yasm：ffmpeg 与 openh264 将降级为不使用 x86 汇编（编码性能略降）")
        self.logger.info(f"Linux 工具链: CC={self.cc} CXX={self.cxx} nasm={self.nasm or '-'}")

    @property
    def marker_variant(self) -> str:
        """Linux 没有类似 CRT/API 级别/最低版本的硬约束，版本文件名里不追加关键信息。"""
        return ""

    def build_env(self, abi: str) -> Dict[str, str]:
        env = os.environ.copy()
        env.update({"CC": self.cc, "CXX": self.cxx,
                    "CFLAGS": "-fPIC -O3", "CXXFLAGS": "-fPIC -O3"})
        return env

    def cmake_args(self, abi: str) -> List[str]:
        return ["-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_POSITION_INDEPENDENT_CODE=ON"]


# ------------------------------------------------------------------ macOS
# 本机 macOS 构建（Xcode clang + make）：用于在 Mac 上直接跑内核单测 / 命令行 demo
# （产物能在宿主执行，比走 iOS 模拟器或 Android 模拟器快一个数量级）。
# 与 Linux 一样是 abi 目录布局：<库>/macos/<abi>/{include,lib}，不做 lipo 合并。
MACOS_ABI = {
    "arm64": {"arch": "arm64", "openssl": "darwin64-arm64-cc", "openh264": "arm64"},
    "x86_64": {"arch": "x86_64", "openssl": "darwin64-x86_64-cc", "openh264": "x86_64"},
}
MACOS_HOST_ARCH = {"arm64": "arm64", "x86_64": "x86_64"}


class MacOSToolchain(Toolchain):
    name = "macos"
    default_abis = [MACOS_HOST_ARCH.get(platform_mod.machine(), "arm64")]
    supported_abis = list(MACOS_ABI)
    use_abi_dir = True
    default_macos_min = "11.0"

    def __init__(self, ctx, min_version: str = ""):
        super().__init__(ctx)
        self.min_version = min_version or self.default_macos_min
        self.compiler_id = "clang"
        self.nasm = which("nasm") or ""
        self._sdk = ""

    def prepare(self) -> None:
        if platform_mod.system() != "Darwin":
            raise ToolchainError("macOS", f"macOS 产物需在 Mac 上构建（当前宿主 {platform_mod.system()}）")
        self._sdk = self._xcrun(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
        if not self._sdk:
            raise ToolchainError("macOS SDK", "xcrun 找不到 macosx SDK", "xcode-select --install 或安装完整版 Xcode")
        for tool, hint in (("make", "openssl/openh264/ffmpeg 需要 make"),
                           ("cmake", "srt/fdk-aac/zlib/libyuv 用 CMake 构建"),
                           ("pkg-config", "ffmpeg 的 configure 靠它找 openssl/srt/fdk-aac/openh264"),
                           ("perl", "openssl 的 Configure 需要 perl")):
            if not which(tool):
                raise ToolchainError(tool, f"未找到 {tool}（{hint}）", f"brew install {tool}")
        if "x86_64" in self.ctx.abis and not self.nasm:
            self.logger.warn("未找到 nasm：x86_64 的 ffmpeg / openh264 将关闭汇编（编码性能略降）")
        self.logger.info(f"macOS 工具链就绪: SDK={self._sdk} min={self.min_version} nasm={self.nasm or '-'}")

    @staticmethod
    def _xcrun(cmd: List[str]) -> str:
        import subprocess
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        except Exception:      # noqa: BLE001 —— 由 prepare() 统一报错
            return ""

    @property
    def sdk_path(self) -> str:
        if not self._sdk:
            self._sdk = self._xcrun(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
        return self._sdk

    @property
    def marker_variant(self) -> str:
        return f"min{self.min_version}"

    def arch_flags(self, abi: str) -> str:
        return f"-arch {MACOS_ABI[abi]['arch']} -mmacosx-version-min={self.min_version}"

    def build_env(self, abi: str) -> Dict[str, str]:
        env = os.environ.copy()
        flags = f"{self.arch_flags(abi)} -isysroot {self.sdk_path} -fPIC -O3"
        env.update({"CC": "clang", "CXX": "clang++", "CFLAGS": flags, "CXXFLAGS": flags,
                    "LDFLAGS": self.arch_flags(abi), "SDKROOT": self.sdk_path,
                    "MACOSX_DEPLOYMENT_TARGET": self.min_version})
        return env

    def cmake_args(self, abi: str) -> List[str]:
        return ["-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
                f"-DCMAKE_OSX_ARCHITECTURES={MACOS_ABI[abi]['arch']}",
                f"-DCMAKE_OSX_DEPLOYMENT_TARGET={self.min_version}",
                f"-DCMAKE_SYSTEM_PROCESSOR={MACOS_ABI[abi]['arch']}"]


TOOLCHAINS = {"windows": WindowsToolchain, "android": AndroidToolchain,
              "ios": IOSToolchain, "ohos": OHOSToolchain, "linux": LinuxToolchain,
              "macos": MacOSToolchain}
# 注意 mac / darwin 历史上指向 ios，保留兼容；macOS 本机产物用 macos / osx / macosx
PLATFORM_ALIASES = {"win": "windows", "win64": "windows", "mac": "ios", "iphoneos": "ios", "darwin": "ios",
                    "osx": "macos", "macosx": "macos",
                    "openharmony": "ohos", "harmonyos": "ohos", "harmony": "ohos", "oh": "ohos",
                    "linux64": "linux", "gnu-linux": "linux"}


def normalize_platform(name: str) -> str:
    key = PLATFORM_ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in TOOLCHAINS:
        raise KeyError(f"未知平台: {name}，可选: {', '.join(TOOLCHAINS)}")
    return key


def create_toolchain(name: str, ctx, **kwargs) -> Toolchain:
    cls = TOOLCHAINS[normalize_platform(name)]
    if cls is WindowsToolchain:
        return cls(ctx, prefer=kwargs.get("toolchain", "auto"))
    if cls is AndroidToolchain:
        return cls(ctx, ndk=kwargs.get("ndk"), api=kwargs.get("api", 21))
    if cls is OHOSToolchain:
        return cls(ctx, sdk=kwargs.get("ohos_sdk"))
    if cls is LinuxToolchain:      # 本机编译，没有 SDK / 最低版本之类的平台参数
        return cls(ctx)
    if cls is MacOSToolchain:      # --min-version 对 macOS 指 -mmacosx-version-min；iOS 的默认值 12.0 不适用
        mv = kwargs.get("min_version")
        return cls(ctx, min_version=mv if mv and mv != "12.0" else "")
    return cls(ctx, min_version=kwargs.get("min_version", "12.0"))
