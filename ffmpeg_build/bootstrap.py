"""工具链自检与自动补齐。

原则：``check`` 只探测不改机器；``install`` 才下载，优先系统包管理器，
需要管理员授权的步骤只下载并打印命令，不会让脚本卡住。
"""

from __future__ import annotations

import os
import platform as pl
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import PROJECT_ROOT, TOOLS_DIR
from .toolchains import MSVC, MSYS2_CANDIDATES
from .utils import CommandRunner, Logger, download_file, extract_archive, which

VS_BUILDTOOLS_WINGET = "Microsoft.VisualStudio.2022.BuildTools"
VS_BUILDTOOLS_URL = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
NASM_URL = "https://www.nasm.us/pub/nasm/releasebuilds/2.16.03/win64/nasm-2.16.03-win64.zip"
CMAKE_URL = "https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6-windows-x86_64.zip"
STRAWBERRY_URL = "https://strawberryperl.com/download/5.32.1.1/strawberry-perl-5.32.1.1-64bit-portable.zip"
NDK_VERSION = "r27c"
NDK_URL = "https://dl.google.com/android/repository/android-ndk-{version}-{host}.zip"


@dataclass
class ToolStatus:
    name: str
    found: bool
    path: str = ""
    hint: str = ""
    # 可选工具：缺了不阻断构建，配方会自动降级（如无 nasm 时关掉 x86 汇编）
    optional: bool = False

    @property
    def mark(self) -> str:
        return "OK  " if self.found else "MISS"


@dataclass
class CheckResult:
    platform: str
    statuses: List[ToolStatus] = field(default_factory=list)

    @property
    def missing(self) -> List[ToolStatus]:
        return [s for s in self.statuses if not s.found and not s.optional]

    @property
    def optional_missing(self) -> List[ToolStatus]:
        return [s for s in self.statuses if not s.found and s.optional]


def tools_root() -> Path:
    return PROJECT_ROOT / TOOLS_DIR


# ------------------------------------------------------------------- 检查
def check(platform: str, logger: Logger, ctx=None) -> CheckResult:
    result = CheckResult(platform=platform)
    abis = list(getattr(ctx, "abis", None) or [])
    if platform == "windows":
        result.statuses = _check_windows(logger, abis)
    elif platform == "android":
        result.statuses = _check_android(ctx)
    elif platform == "ohos":
        result.statuses = _check_ohos(ctx)
    elif platform == "linux":
        result.statuses = _check_linux()
    elif platform == "macos":
        # 与 iOS 共用 Xcode / cmake / pkg-config 检查；nasm 只有 x86_64 需要
        result.statuses = _check_ios([a for a in abis if a == "x86_64"] or ["arm64"])
    else:
        result.statuses = _check_ios(abis)
    return result


def _check_windows(logger: Logger, abis: Optional[List[str]] = None) -> List[ToolStatus]:
    from .toolchains import WINDOWS_ABI, msvc_targets, vcvars_arch
    tools = tools_root()
    abis = abis or ["x64"]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(tools / "nasm"), str(tools / "cmake" / "bin"), env.get("PATH", "")])
    bash = next((str(Path(r) / "usr" / "bin" / "bash.exe") for r in MSYS2_CANDIDATES
                 if (Path(r) / "usr" / "bin" / "bash.exe").exists()), "")
    # 每个目标 ABI 单独核对一次：只装了 x64 组件时，x86/arm64 必须在自检阶段就报出来
    statuses = []
    for abi in abis:
        msvc = MSVC.discover(logger, vcvars_arch(abi))
        ok = msvc_targets(msvc, abi)
        component = ("Microsoft.VisualStudio.Component.VC.Tools.ARM64" if abi == "arm64"
                     else "Microsoft.VisualStudio.Component.VC.Tools.x86.x64")
        statuses.append(ToolStatus(f"MSVC[{abi}](VS Build Tools)", ok, str(msvc.install) if ok else "",
                                   f"winget install {VS_BUILDTOOLS_WINGET} 或 {VS_BUILDTOOLS_URL}；"
                                   f"需要组件 {component}"))
    statuses += [
        ToolStatus("cmake", bool(which("cmake", env)), which("cmake", env) or "", "winget install Kitware.CMake"),
        ToolStatus("perl(原生Windows)", bool(detect_native_perl()), detect_native_perl() or "",
                   "python build.py bootstrap --platform windows --install（便携版 Strawberry Perl）"),
        # nasm 只有 x86/x64 的汇编要用；只编 arm64 时不阻断
        ToolStatus("nasm", bool(which("nasm", env)), which("nasm", env) or "", "winget install NASM.NASM",
                   optional=not any(WINDOWS_ABI[a]["nasm"] for a in abis)),
        ToolStatus("GNU make(MSYS2)", bool(which("make", env) or bash), which("make", env) or bash,
                   "MSYS2: pacman -S make perl nasm"),
    ]
    if "arm64" in abis:
        gas = which("gas-preprocessor.pl", env) or ""
        statuses.append(ToolStatus("gas-preprocessor.pl", bool(gas), gas,
                                   "arm64 汇编需要（放进 PATH）；缺失时 ffmpeg --disable-asm、openh264 USE_ASM=No",
                                   optional=True))
    return statuses


def _check_android(ctx) -> List[ToolStatus]:
    ndk = None
    if ctx is not None and getattr(ctx, "toolchain", None) is not None:
        try:
            ndk = ctx.toolchain._detect_ndk()
        except Exception:  # noqa: BLE001
            ndk = None
    return [
        ToolStatus("Android NDK", bool(ndk), str(ndk) if ndk else "",
                   "设置 ANDROID_NDK_HOME 或 bootstrap --platform android --install"),
        ToolStatus("cmake", bool(which("cmake")), which("cmake") or "", _host_install_hint("cmake")),
        ToolStatus("make", bool(which("make")), which("make") or "", _host_install_hint("make")),
        # openssl 的 Configure 是 perl 脚本；ffmpeg 的 configure 靠 pkg-config 找
        # openssl / srt / fdk-aac / openh264 —— 缺了要拖到编译中途才暴露
        ToolStatus("perl", bool(which("perl")), which("perl") or "", _host_install_hint("perl")),
        ToolStatus("pkg-config", bool(which("pkg-config")), which("pkg-config") or "",
                   _host_install_hint("pkg-config")),
    ]


def _host_install_hint(tool: str) -> str:
    """交叉编译宿主（macOS / Linux）上装工具的命令。"""
    if pl.system() == "Darwin":
        return f"brew install {tool}"
    package = {"pkg-config": "pkgconf-pkg-config"}.get(tool, tool)
    return f"apt install -y {tool} / dnf install -y {package}"


def _check_ohos(ctx) -> List[ToolStatus]:
    sdk = ""
    if ctx is not None and getattr(ctx, "toolchain", None) is not None:
        try:
            sdk = str(ctx.toolchain.detect_sdk() or "")
        except Exception:  # noqa: BLE001
            sdk = ""
    env = dict(os.environ)
    if sdk:
        from pathlib import Path
        env["PATH"] = os.pathsep.join([str(Path(sdk) / "native" / "llvm" / "bin"),
                                       str(Path(sdk) / "native" / "build-tools" / "cmake" / "bin"),
                                       env.get("PATH", "")])
    sdk_cmake = _ohos_sdk_cmake(sdk)
    return [
        ToolStatus("OHOS SDK", bool(sdk), sdk,
                   "安装 DevEco Studio/commandline-tools 后 export OHOS_SDK=<.../sdk/default/openharmony>"),
        ToolStatus("cmake(SDK 自带优先)", bool(sdk_cmake or which("cmake", env)),
                   sdk_cmake or which("cmake", env) or "", "apt install cmake / brew install cmake"),
        ToolStatus("make", bool(which("make", env)), which("make", env) or "", "apt install make / brew install make"),
        ToolStatus("perl", bool(which("perl", env)), which("perl", env) or "",
                   "openssl 的 Configure 需要 perl：apt install perl"),
        ToolStatus("bash(宿主)", pl.system() != "Windows" or bool(which("bash", env)), pl.system(),
                   "OHOS 交叉编译依赖 bash/GNU make；Windows 请装 MSYS2/Git Bash 或改用 WSL"),
    ]


def _ohos_sdk_cmake(sdk: str) -> str:
    if not sdk:
        return ""
    from pathlib import Path
    candidate = Path(sdk) / "native" / "build-tools" / "cmake" / "bin" / "cmake"
    return str(candidate) if candidate.exists() else ""


def _check_linux() -> List[ToolStatus]:
    """本机 Linux：gcc/clang + make + cmake + pkg-config + perl，nasm 可选。"""
    on_linux = pl.system() == "Linux"
    cc = which("gcc") or which("clang") or which("cc") or ""
    asm = which("nasm") or which("yasm") or ""
    return [
        ToolStatus("Linux 宿主", on_linux, pl.platform(), "Linux 产物需在 Linux 上构建"),
        ToolStatus("gcc/clang", bool(cc), cc, "dnf install -y gcc gcc-c++ / apt install -y build-essential"),
        ToolStatus("cmake", bool(which("cmake")), which("cmake") or "",
                   "srt/fdk-aac/zlib/libyuv 用 CMake 构建：dnf install -y cmake / apt install -y cmake"),
        ToolStatus("make", bool(which("make")), which("make") or "",
                   "openssl/openh264/ffmpeg 需要 GNU make：dnf install -y make / apt install -y make"),
        # ffmpeg 的 configure 靠 pkg-config 找 openssl / srt / fdk-aac / openh264，
        # 缺了不会在自检阶段暴露，而是拖到 configure 才报 "xxx not found using pkg-config"
        ToolStatus("pkg-config", bool(which("pkg-config")), which("pkg-config") or "",
                   "dnf install -y pkgconf-pkg-config / apt install -y pkg-config"),
        ToolStatus("perl", bool(which("perl")), which("perl") or "",
                   "openssl 的 Configure 需要 perl：dnf install -y perl / apt install -y perl"),
        ToolStatus("nasm", bool(asm), asm,
                   "x86 汇编（ffmpeg/openh264）需要；缺失时自动 --disable-x86asm", optional=True),
    ]


def _check_ios(abis: Optional[List[str]] = None) -> List[ToolStatus]:
    on_mac = pl.system() == "Darwin"
    xcode = _run(["xcode-select", "-p"]) if on_mac else ""
    abis = abis or ["arm64", "x86_64"]
    # nasm 只给 x86_64 模拟器的 openh264 汇编用；只编 arm64 真机时不该阻断
    # （与 IOSToolchain.prepare 的判断保持一致）
    need_nasm = "x86_64" in abis
    return [
        ToolStatus("macOS", on_mac, pl.system(), "iOS 只能在 macOS 上交叉编译"),
        ToolStatus("Xcode", bool(xcode), xcode, "xcode-select --install"),
        ToolStatus("cmake", bool(which("cmake")), which("cmake") or "", "brew install cmake"),
        ToolStatus("nasm", bool(which("nasm")), which("nasm") or "",
                   "brew install nasm（x86_64 模拟器的 openh264 需要）", optional=not need_nasm),
        # ffmpeg 的 configure 靠 pkg-config 找 openssl / srt / fdk-aac / openh264，
        # 缺了不会在自检阶段暴露，而是拖到 configure 才报 "xxx not found using pkg-config"
        ToolStatus("pkg-config", bool(which("pkg-config")), which("pkg-config") or "", "brew install pkg-config"),
    ]


def report(result: CheckResult, logger: Logger) -> None:
    logger.section(f"{result.platform} 工具链自检")
    for status in result.statuses:
        note = "（可选，缺失会自动降级）" if (status.optional and not status.found) else ""
        logger.info(f"{status.mark} {status.name:<22} {status.path}{note}")
    for status in result.missing:
        logger.warn(f"缺少 {status.name}: {status.hint}")
    for status in result.optional_missing:
        logger.warn(f"可选工具缺失 {status.name}: {status.hint}")
    if not result.missing:
        logger.info("工具链完整，可以开始编译")


# ------------------------------------------------------------------- 安装
def install(platform: str, result: CheckResult, logger: Logger, runner: CommandRunner) -> List[str]:
    actions: List[str] = []
    tools = tools_root()
    tools.mkdir(parents=True, exist_ok=True)
    msvc_done = False
    for status in result.missing:
        if platform == "windows":
            if status.name.startswith("MSVC"):
                # 多个 ABI 缺 MSVC 时只装一次（组件一次性带齐）
                if msvc_done:
                    continue
                msvc_done = True
            actions += _install_windows(status, logger, runner, tools)
        elif platform == "android":
            actions += _install_android(status, logger, runner, tools)
        elif platform == "ohos":
            actions += _install_ohos(status)
        elif platform == "linux":
            actions += _install_linux(status)
        else:
            actions += _install_ios(status, logger, runner)
    return actions


def _install_windows(status: ToolStatus, logger: Logger, runner: CommandRunner, tools: Path) -> List[str]:
    name = status.name
    if name.startswith("MSVC"):
        return _install_msvc(logger, runner, tools)
    if name.startswith("perl"):
        archive = download_file(STRAWBERRY_URL, tools / "strawberry-perl-portable.zip", logger)
        extract_archive(archive, tools, logger)
        return [f"Strawberry Perl -> {tools / 'perl' / 'bin'}"]
    if name == "nasm":
        extract_archive(download_file(NASM_URL, tools / "nasm.zip", logger), tools / "nasm", logger, 1)
        return [f"nasm -> {tools / 'nasm'}"]
    if name == "cmake":
        extract_archive(download_file(CMAKE_URL, tools / "cmake.zip", logger), tools / "cmake", logger, 1)
        return [f"cmake -> {tools / 'cmake' / 'bin'}"]
    return [f"{name} 缺失：安装 MSYS2 后 pacman -S make perl nasm"]


def _install_msvc(logger: Logger, runner: CommandRunner, tools: Path) -> List[str]:
    # x86/x64 与 ARM64 两套编译器组件一起装：--abi x86 / arm64 都不用再回来补
    components = ("--add Microsoft.VisualStudio.Workload.VCTools "
                  "--add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 "
                  "--add Microsoft.VisualStudio.Component.VC.Tools.ARM64 ")
    if which("winget"):
        logger.step("尝试 winget 安装 VS Build Tools（可能需要管理员授权）")
        result = runner.run(["winget", "install", "--id", VS_BUILDTOOLS_WINGET, "--exact",
                             "--accept-source-agreements", "--accept-package-agreements",
                             "--disable-interactivity", "--override",
                             "--wait --passive " + components +
                             "--add Microsoft.VisualStudio.Component.Windows11SDK.22621 --includeRecommended"],
                            check=False, log_name="winget-vs")
        if result.returncode == 0:
            return ["VS Build Tools 安装完成"]
    installer = download_file(VS_BUILDTOOLS_URL, tools / "vs_BuildTools.exe", logger)
    return [f"已下载安装器: {installer}",
            f'管理员执行: "{installer}" --passive --wait {components}--includeRecommended',
            "已装 VS 但缺某个架构：VS Installer → 修改 → 单个组件，勾选对应的 MSVC 生成工具"]


def _install_android(status: ToolStatus, logger: Logger, runner: CommandRunner, tools: Path) -> List[str]:
    if status.name == "Android NDK":
        host = {"Darwin": "darwin", "Windows": "windows"}.get(pl.system(), "linux")
        zip_name = f"android-ndk-{NDK_VERSION}-{host}.zip"
        extract_archive(download_file(NDK_URL.format(version=NDK_VERSION, host=host), tools / zip_name, logger),
                        tools, logger)
        return [f"NDK -> {tools / f'android-ndk-{NDK_VERSION}'}"]
    if pl.system() == "Darwin" and which("brew"):
        package = {"perl": "perl", "pkg-config": "pkg-config", "cmake": "cmake", "make": "make"}.get(status.name)
        if package:
            runner.run(["brew", "install", package], check=False, log_name=f"brew-{package}")
            return [f"已执行 brew install {package}"]
    # Linux 装包需要 root，只给命令不代装（与 linux 平台的 bootstrap 一致）
    return [f"{status.name} 缺失：{status.hint}"]


def _install_ohos(status: ToolStatus) -> List[str]:
    """OHOS SDK 只能由 DevEco Studio / commandline-tools 下载，这里只给指引。"""
    return [f"{status.name} 缺失：{status.hint}"]


def _install_linux(status: ToolStatus) -> List[str]:
    """只给安装命令：装包需要 root，脚本不代劳（避免半途卡在 sudo 密码上）。"""
    packages = {"gcc/clang": "gcc gcc-c++", "cmake": "cmake", "make": "make",
                "pkg-config": "pkgconf-pkg-config", "perl": "perl", "nasm": "nasm"}
    package = packages.get(status.name, status.name)
    return [f"{status.name} 缺失：dnf install -y {package}（或 apt install -y {package}）"]


def _install_ios(status: ToolStatus, logger: Logger, runner: CommandRunner) -> List[str]:
    if status.name == "macOS":
        return ["iOS 依赖库必须在 macOS 上编译"]
    if status.name == "Xcode":
        runner.run(["xcode-select", "--install"], check=False)
        return ["已触发 Xcode 命令行工具安装，完成后重试"]
    package = {"cmake": "cmake", "nasm": "nasm", "pkg-config": "pkg-config"}.get(status.name)
    if package and which("brew"):
        runner.run(["brew", "install", package], check=False, log_name=f"brew-{package}")
        return [f"已执行 brew install {package}"]
    return [f"{status.name} 缺失：brew install {status.name.lower()}"]


# ------------------------------------------------------------------- 辅助
def detect_native_perl(extra_dirs: Optional[List[str]] = None) -> Optional[str]:
    """openssl 的 Windows 构建要求 perl 的 $^O 为 MSWin32。

    MSYS2 自带的是 cygwin perl，会被 openssl 拒绝，因此优先用便携版 Strawberry Perl。
    """
    candidates = [str(tools_root() / "perl" / "bin" / "perl.exe")]
    candidates += [str(Path(d) / "perl" / "bin" / "perl.exe") for d in (extra_dirs or [])]
    system_perl = which("perl")
    if system_perl:
        candidates.append(system_perl)
    for candidate in candidates:
        if Path(candidate).exists() and _is_mswin32(candidate):
            return str(Path(candidate))
    return None


def _is_mswin32(perl: str) -> bool:
    try:
        proc = subprocess.run([perl, "-e", "print $^O;"], capture_output=True,
                              text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip().lower().startswith("mswin32")


def _run(cmd: List[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def ensure_ready(platform: str, logger: Logger, runner: CommandRunner, ctx=None, auto_install: bool = False) -> None:
    result = check(platform, logger, ctx)
    report(result, logger)
    if result.missing and auto_install:
        for action in install(platform, result, logger, runner):
            logger.info(action)
        report(check(platform, logger, ctx), logger)
    if check(platform, logger, ctx).missing:
        raise RuntimeError(f"{platform} 工具链不完整，执行: python build.py bootstrap --platform {platform} --install")
