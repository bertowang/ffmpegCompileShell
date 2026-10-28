"""多 ABI / 白名单校验 / CMake 包配置的回归测试。

运行: ``python -m unittest discover -s tests -t .``
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ffmpeg_build import bootstrap, recipes
from ffmpeg_build.cli import (CMAKE_PACKAGE_NAME, CMAKE_PACKAGE_VERSION, build_parser,
                              export_cmake_package)
from ffmpeg_build.toolchains import (WINDOWS_ABI, AndroidToolchain, IOSToolchain, ToolchainError,
                                     WindowsToolchain, msvc_targets, vcvars_arch)
from ffmpeg_build.utils import Logger


def _ctx(platform, abis=None, **extra):
    base = dict(platform=platform, abis=abis or [], output_root=Path("out"), crt="mt",
                logger=Logger(verbose=False), runner=None, enable_nvenc=False, enable_amf=False)
    base.update(extra)
    return SimpleNamespace(**base)


class ExpandAbisTest(unittest.TestCase):
    def test_windows_comma_space_all(self):
        tc = WindowsToolchain(_ctx("windows"))
        self.assertEqual(tc.expand_abis(None), ["x64"])
        self.assertEqual(tc.expand_abis(["x64,x86"]), ["x64", "x86"])       # 逗号
        self.assertEqual(tc.expand_abis(["x86", "x64"]), ["x86", "x64"])    # 空格（nargs=*）保序
        self.assertEqual(tc.expand_abis(["x64, x86", "x64"]), ["x64", "x86"])  # 去重
        self.assertEqual(tc.expand_abis(["all"]), list(WINDOWS_ABI))

    def test_invalid_abi_raises(self):
        # 以前非法值会被悄悄丢掉、回落成 x64
        tc = WindowsToolchain(_ctx("windows"))
        for bad in (["x86_64"], ["x64,armv7"], ["Win32"]):
            with self.assertRaises(ToolchainError, msg=bad):
                tc.expand_abis(bad)

    def test_android_ios_aliases(self):
        android = AndroidToolchain(_ctx("android"))
        self.assertEqual(android.expand_abis(["all"]), ["arm64-v8a", "armeabi-v7a"])
        self.assertEqual(android.expand_abis(["arm64-v8a,x86_64"]), ["arm64-v8a", "x86_64"])
        with self.assertRaises(ToolchainError):
            android.expand_abis(["arm64"])
        ios = IOSToolchain(_ctx("ios"))
        self.assertEqual(ios.expand_abis(["fat"]), ["arm64", "x86_64"])
        self.assertEqual(ios.expand_abis(["device"]), ["arm64"])

    def test_cli_accepts_comma(self):
        args = build_parser().parse_args(["build", "-p", "windows", "--abi", "x64,x86", "-o", "D:/out"])
        self.assertEqual(args.abi, ["x64,x86"])


class WindowsAbiTest(unittest.TestCase):
    def test_vcvars_arch(self):
        self.assertEqual(vcvars_arch("x64", host="x64"), "x64")
        self.assertEqual(vcvars_arch("x86", host="x64"), "x64_x86")
        self.assertEqual(vcvars_arch("arm64", host="x64"), "x64_arm64")
        self.assertEqual(vcvars_arch("arm64", host="arm64"), "arm64")

    def test_msvc_targets_checks_cl_dir(self):
        x64_cl = r"C:\VS\VC\Tools\MSVC\14.44\bin\Hostx64\x64\cl.exe"
        x86_cl = r"C:\VS\VC\Tools\MSVC\14.44\bin\Hostx64\x86\cl.exe"
        self.assertFalse(msvc_targets(None, "x64"))
        # 只装了 x64 组件：vcvarsall x64_x86 仍可能返回 x64 的 cl，必须判为不可用
        for cl, abi, expect in ((x64_cl, "x64", True), (x64_cl, "x86", False), (x86_cl, "x86", True)):
            msvc = SimpleNamespace(env={"PATH": str(Path(cl).parent)}, install=Path("C:/VS"))
            with mock.patch("ffmpeg_build.toolchains.which",
                            side_effect=lambda name, env=None, _cl=cl: _cl if name == "cl" else None):
                self.assertEqual(msvc_targets(msvc, abi), expect, (cl, abi))

    def test_cmake_platform_per_abi(self):
        for abi, platform in (("x64", "x64"), ("x86", "Win32"), ("arm64", "ARM64")):
            tc = WindowsToolchain(_ctx("windows", [abi]))
            tc.ninja = ""
            tc.msvc = SimpleNamespace(env={}, generator="Visual Studio 17 2022")
            args = tc.cmake_args(abi)
            self.assertEqual(args[:4], ["-G", "Visual Studio 17 2022", "-A", platform])

    def test_ffmpeg_args_per_abi(self):
        stub = SimpleNamespace(build_env=lambda abi: {"PATH": ""})
        with mock.patch("ffmpeg_build.toolchains.windows_host_arch", return_value="x64"):
            x86 = recipes._ffmpeg_platform_args(_ctx("windows", toolchain=stub), "x86")
            arm = recipes._ffmpeg_platform_args(_ctx("windows", toolchain=stub), "arm64")
        self.assertIn("--arch=x86", x86)
        self.assertIn("--target-os=win32", x86)
        self.assertNotIn("--enable-cross-compile", x86)        # x64 宿主能跑 x86 测试程序
        self.assertIn("--arch=aarch64", arm)
        self.assertIn("--enable-cross-compile", arm)
        self.assertIn("--disable-asm", arm)                    # 没有 gas-preprocessor.pl


class BootstrapTest(unittest.TestCase):
    def _windows(self, abis):
        msvc = SimpleNamespace(install=Path("C:/VS"))
        with mock.patch.object(bootstrap.MSVC, "discover", return_value=msvc), \
                mock.patch("ffmpeg_build.toolchains.msvc_targets", return_value=True):
            return {s.name: s for s in bootstrap._check_windows(Logger(verbose=False), abis)}

    def test_windows_checks_each_abi(self):
        statuses = self._windows(["x64", "arm64"])
        self.assertIn("MSVC[x64](VS Build Tools)", statuses)
        self.assertIn("MSVC[arm64](VS Build Tools)", statuses)
        self.assertTrue(statuses["gas-preprocessor.pl"].optional)

    def test_nasm_optional_only_for_arm(self):
        self.assertFalse(self._windows(["x64"])["nasm"].optional)
        self.assertTrue(self._windows(["arm64"])["nasm"].optional)
        ios = {s.name.split("（")[0]: s for s in bootstrap._check_ios(["arm64"])}
        nasm = next(s for n, s in ios.items() if n.startswith("nasm"))
        self.assertTrue(nasm.optional)                         # 只编真机 arm64 不要求 nasm
        ios = bootstrap._check_ios(["arm64", "x86_64"])
        self.assertFalse(next(s for s in ios if s.name.startswith("nasm")).optional)

    def test_android_checks_perl_pkgconfig(self):
        names = [s.name for s in bootstrap._check_android(None)]
        self.assertIn("perl", names)
        self.assertIn("pkg-config", names)


class WhitelistValidationTest(unittest.TestCase):
    def _source(self, root: Path, demuxers):
        fmt = root / "libavformat"
        fmt.mkdir(parents=True)
        (fmt / "allformats.c").write_text(
            "".join(f"extern const FFInputFormat  ff_{d}_demuxer;\n" for d in demuxers), encoding="utf-8")
        return root

    def test_unknown_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._source(Path(tmp), ["mov", "flv", "matroska", "mpegts", "hls", "image2"])
            ctx = _ctx("windows", profile="player")
            recipes.validate_whitelist(ctx, src)               # 真实名字全部通过
            real = recipes.C.whitelist("player")
            bad = dict(real, demuxers=["mov", "mkv", "mp4"])
            with mock.patch.object(recipes.C, "whitelist", return_value=bad):
                with self.assertRaises(RuntimeError) as cm:
                    recipes.validate_whitelist(ctx, src)
            self.assertIn("mkv", str(cm.exception))
            self.assertIn("mp4", str(cm.exception))

    def test_missing_or_unparsable_registry_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx("windows", profile="player")
            recipes.validate_whitelist(ctx, Path(tmp))          # 源码未就绪：跳过
            src = Path(tmp) / "src"
            (src / "libavformat").mkdir(parents=True)
            (src / "libavformat" / "allformats.c").write_text("/* 格式变了 */", encoding="utf-8")
            recipes.validate_whitelist(ctx, src)               # 解析不出组件：只告警


class CMakePackageTest(unittest.TestCase):
    def test_export_writes_config_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = export_cmake_package(Path(tmp), Logger(verbose=False))
            names = sorted(p.name for p in files)
            self.assertEqual(names, [f"{CMAKE_PACKAGE_NAME}Config.cmake",
                                     f"{CMAKE_PACKAGE_NAME}ConfigVersion.cmake"])
            version = (Path(tmp) / names[1]).read_text(encoding="utf-8")
            self.assertIn(f'set(PACKAGE_VERSION "{CMAKE_PACKAGE_VERSION}")', version)

    def test_cli_export(self):
        args = build_parser().parse_args(["export", "-o", "D:/out"])
        self.assertEqual(args.func.__name__, "cmd_export")

    @unittest.skipUnless(shutil.which("cmake"), "需要 cmake")
    def test_find_package_static_tree(self):
        """伪造一份 linux 静态产物，用 cmake 实际跑 find_package，核对导入目标与 .pc 依赖传递。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "3rd"
            abi = "x86_64"
            for lib, name in (("zlib", "libz.a"), ("openssl", "libssl.a"), ("openssl", "libcrypto.a"),
                              ("srt", "libsrt.a"), ("fdk-aac", "libfdk-aac.a"),
                              ("openh264", "libopenh264.a"), ("libyuv", "libyuv.a"),
                              ("ffmpeg", "libavutil.a"), ("ffmpeg", "libavformat.a")):
                d = root / lib / "linux" / abi
                (d / "include").mkdir(parents=True, exist_ok=True)
                (d / "lib").mkdir(exist_ok=True)
                (d / "lib" / name).write_bytes(b"")
            pc = root / "ffmpeg" / "linux" / abi / "lib" / "pkgconfig"
            pc.mkdir()
            (pc / "libavutil.pc").write_text("Name: libavutil\nRequires:\nLibs: -L${libdir} -lavutil -pthread -lm\n")
            (pc / "libavformat.pc").write_text(
                "Name: libavformat\nRequires: libavutil >= 58\n"
                "Libs: -L${libdir} -lavformat -L/old/abs/path -lssl -lcrypto -lsrt -lz\nLibs.private:\n")
            export_cmake_package(root, Logger(verbose=False))
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\nproject(p NONE)\n"
                "set(TGCapture3rd_PLATFORM linux)\nset(TGCapture3rd_ABI x86_64)\n"
                "find_package(TGCapture3rd 1 CONFIG REQUIRED COMPONENTS ffmpeg libyuv)\n"
                "get_target_property(d TGCapture3rd::avformat INTERFACE_LINK_LIBRARIES)\n"
                "message(STATUS \"DEPS=${d}\")\n"
                "find_package(TGCapture3rd 2 CONFIG QUIET)\n"
                "message(STATUS \"V2=${TGCapture3rd_FOUND}\")\n", encoding="utf-8")
            out = subprocess.run(["cmake", "-S", str(proj), "-B", str(proj / "b"),
                                  f"-DCMAKE_PREFIX_PATH={root.as_posix()}"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            deps = next(line for line in out.stdout.splitlines() if "DEPS=" in line)
            for expect in ("TGCapture3rd::avutil", "TGCapture3rd::ssl", "TGCapture3rd::crypto",
                           "TGCapture3rd::srt", "TGCapture3rd::zlib"):
                self.assertIn(expect, deps)
            self.assertNotIn("/old/abs/path", deps)            # 旧绝对路径（-L）不带出
            self.assertIn("V2=0", out.stdout)                  # 主版本不同不兼容


if __name__ == "__main__":
    unittest.main()
