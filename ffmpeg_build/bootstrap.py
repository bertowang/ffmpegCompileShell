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

    @property
    def mark(self) -> str:
        return "OK  " if self.found else "MISS"


@dataclass
class CheckResult:
    platform: str
    statuses: List[ToolStatus] = field(default_factory=list)

    @property
    def missing(self) -> List[ToolStatus]:
        return [s for s in self.statuses if not s.found]


def tools_root() -> Path:
    return PROJECT_ROOT / TOOLS_DIR


# ------------------------------------------------------------------- 检查
def check(platform: str, logger: Logger, ctx=None) -> CheckResult:
    result = CheckResult(platform=platform)
    if platform == "windows":
        result.statuses = _check_windows(logger)
    elif platform == "android":
        result.statuses = _check_android(ctx)
    elif platform == "ohos":
        result.statuses = _check_ohos(ctx)
    else:
        result.statuses = _check_ios()
    return result


def _check_windows(logger: Logger) -> List[ToolStatus]:
    tools = tools_root()
    msvc = MSVC.discover(logger)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(tools / "nasm"), str(tools / "cmake" / "bin"), env.get("PATH", "")])
    bash = next((str(Path(r) / "usr" / "bin" / "bash.exe") for r in MSYS2_CANDIDATES
                 if (Path(r) / "usr" / "bin" / "bash.exe").exists()), "")
    return [
        ToolStatus("MSVC(VS Build Tools)", bool(msvc), str(msvc.install) if msvc else "",
                   f"winget install {VS_BUILDTOOLS_WINGET} 或 {VS_BUILDTOOLS_URL}"),
        ToolStatus("cmake", bool(which("cmake", env)), which("cmake", env) or "", "winget install Kitware.CMake"),
        ToolStatus("perl(原生Windows)", bool(detect_native_perl()), detect_native_perl() or "",
                   "python build.py bootstrap --platform windows --install（便携版 Strawberry Perl）"),
        ToolStatus("nasm", bool(which("nasm", env)), which("nasm", env) or "", "winget install NASM.NASM"),
        ToolStatus("GNU make(MSYS2)", bool(which("make", env) or bash), which("make", env) or bash,
                   "MSYS2: pacman -S make perl nasm"),
    ]


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
        ToolStatus("cmake", bool(which("cmake")), which("cmake") or "", "brew install cmake"),
        ToolStatus("make", bool(which("make")), which("make") or "", "brew install make"),
    ]


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


def _check_ios() -> List[ToolStatus]:
    on_mac = pl.system() == "Darwin"
    xcode = _run(["xcode-select", "-p"]) if on_mac else ""
    return [
        ToolStatus("macOS", on_mac, pl.system(), "iOS 只能在 macOS 上交叉编译"),
        ToolStatus("Xcode", bool(xcode), xcode, "xcode-select --install"),
        ToolStatus("cmake", bool(which("cmake")), which("cmake") or "", "brew install cmake"),
        ToolStatus("nasm", bool(which("nasm")), which("nasm") or "", "brew install nasm"),
        # ffmpeg 的 configure 靠 pkg-config 找 openssl / srt / fdk-aac / openh264，
        # 缺了不会在自检阶段暴露，而是拖到 configure 才报 "xxx not found using pkg-config"
        ToolStatus("pkg-config", bool(which("pkg-config")), which("pkg-config") or "", "brew install pkg-config"),
    ]


def report(result: CheckResult, logger: Logger) -> None:
    logger.section(f"{result.platform} 工具链自检")
    for status in result.statuses:
        logger.info(f"{status.mark} {status.name:<22} {status.path}")
    for status in result.missing:
        logger.warn(f"缺少 {status.name}: {status.hint}")
    if not result.missing:
        logger.info("工具链完整，可以开始编译")


# ------------------------------------------------------------------- 安装
def install(platform: str, result: CheckResult, logger: Logger, runner: CommandRunner) -> List[str]:
    actions: List[str] = []
    tools = tools_root()
    tools.mkdir(parents=True, exist_ok=True)
    for status in result.missing:
        if platform == "windows":
            actions += _install_windows(status, logger, runner, tools)
        elif platform == "android":
            actions += _install_android(status, logger, tools)
        elif platform == "ohos":
            actions += _install_ohos(status)
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
    if which("winget"):
        logger.step("尝试 winget 安装 VS Build Tools（可能需要管理员授权）")
        result = runner.run(["winget", "install", "--id", VS_BUILDTOOLS_WINGET, "--exact",
                             "--accept-source-agreements", "--accept-package-agreements",
                             "--disable-interactivity", "--override",
                             "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools "
                             "--add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 "
                             "--add Microsoft.VisualStudio.Component.Windows11SDK.22621 --includeRecommended"],
                            check=False, log_name="winget-vs")
        if result.returncode == 0:
            return ["VS Build Tools 安装完成"]
    installer = download_file(VS_BUILDTOOLS_URL, tools / "vs_BuildTools.exe", logger)
    return [f"已下载安装器: {installer}",
            f'管理员执行: "{installer}" --passive --wait --add Microsoft.VisualStudio.Workload.VCTools '
            "--add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --includeRecommended"]


def _install_android(status: ToolStatus, logger: Logger, tools: Path) -> List[str]:
    if status.name == "Android NDK":
        host = {"Darwin": "darwin", "Windows": "windows"}.get(pl.system(), "linux")
        zip_name = f"android-ndk-{NDK_VERSION}-{host}.zip"
        extract_archive(download_file(NDK_URL.format(version=NDK_VERSION, host=host), tools / zip_name, logger),
                        tools, logger)
        return [f"NDK -> {tools / f'android-ndk-{NDK_VERSION}'}"]
    return [f"{status.name} 缺失：brew install {status.name}"]


def _install_ohos(status: ToolStatus) -> List[str]:
    """OHOS SDK 只能由 DevEco Studio / commandline-tools 下载，这里只给指引。"""
    return [f"{status.name} 缺失：{status.hint}"]


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
