"""冒烟测试：配置齐全、配方与平台注册一致、CLI 可解析。

运行: ``python -m unittest discover -s tests -t .``
"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from ffmpeg_build import recipes
from ffmpeg_build.cli import build_parser, make_context
from ffmpeg_build.config import (BUILD_ORDER, LIBRARIES, OPTIONAL_LIBRARIES,
                                 resolve_output_root, resolve_selection)
from ffmpeg_build.recipes import REGISTRY
from ffmpeg_build.toolchains import OHOS_ABI, OHOSToolchain, TOOLCHAINS, normalize_platform
from ffmpeg_build.utils import Logger

FORBIDDEN = ("x264", "x265", "v265")


class ConfigTest(unittest.TestCase):
    def test_recipe_for_every_library(self):
        self.assertEqual(set(LIBRARIES), set(REGISTRY))

    def test_build_order_covers_all(self):
        self.assertEqual(set(BUILD_ORDER) | set(OPTIONAL_LIBRARIES), set(LIBRARIES))
        self.assertEqual(BUILD_ORDER[-1], "ffmpeg")   # ffmpeg 依赖其它库，必须最后

    def test_no_forbidden_codecs(self):
        for name in FORBIDDEN:
            self.assertNotIn(name, LIBRARIES)

    def test_optional_libraries(self):
        self.assertIn("spdlog", OPTIONAL_LIBRARIES)
        self.assertNotIn("spdlog", BUILD_ORDER)          # 默认不构建
        self.assertIn("spdlog", REGISTRY)
        # MediaX 的 find_package(spdlog 1.14.1 REQUIRED) 决定了版本不能动
        self.assertEqual(LIBRARIES["spdlog"].version, "v1.14.1")

    def test_selection_default_excludes_optional(self):
        self.assertEqual(resolve_selection(None), list(BUILD_ORDER))
        self.assertEqual(resolve_selection("json"), ["json"])
        self.assertEqual(resolve_selection("ffmpeg,srt"), ["ffmpeg", "srt"])
        with self.assertRaises(KeyError):
            resolve_selection("x264")


class PlatformTest(unittest.TestCase):
    def test_platforms(self):
        self.assertEqual(set(TOOLCHAINS), {"windows", "android", "ios", "ohos"})

    def test_aliases(self):
        self.assertEqual(normalize_platform("openharmony"), "ohos")
        self.assertEqual(normalize_platform("win64"), "windows")


class OHOSTest(unittest.TestCase):
    """OHOS 工具链：不依赖真实 SDK，只验证路径/环境/参数拼装。"""

    def _context(self):
        ctx = SimpleNamespace(logger=Logger(verbose=False), runner=None,
                              abis=["arm64-v8a"], output_root=Path("out"), platform="ohos")
        tc = OHOSToolchain(ctx, sdk="sdk")
        tc.sdk = Path("sdk")
        ctx.toolchain = tc
        return ctx, tc

    def test_abi_table(self):
        self.assertEqual(set(OHOS_ABI), {"arm64-v8a", "armeabi-v7a", "x86_64"})

    def test_build_env(self):
        _, tc = self._context()
        env = tc.build_env("arm64-v8a")
        self.assertTrue(env["CC"].endswith("aarch64-unknown-linux-ohos-clang"))
        self.assertIn("-D__MUSL__=1", env["CFLAGS"])
        self.assertEqual(env["OHOS_ARCH"], "arm64-v8a")

    def test_cmake_args_and_install_dir(self):
        _, tc = self._context()
        self.assertIn("-DOHOS_PLATFORM=OHOS", tc.cmake_args("arm64-v8a"))
        self.assertEqual(tc.install_dir("ffmpeg", "arm64-v8a"),
                         Path("out") / "ffmpeg" / "ohos" / "arm64-v8a")

    def test_expand_abis(self):
        _, tc = self._context()
        self.assertEqual(tc.expand_abis(None), ["arm64-v8a"])
        self.assertEqual(tc.expand_abis(["all"]), ["arm64-v8a", "armeabi-v7a", "x86_64"])

    def test_ffmpeg_cross_args(self):
        ctx, _ = self._context()
        args = recipes._ffmpeg_platform_args(ctx, "arm64-v8a")
        self.assertIn("--target-os=linux", args)
        self.assertIn("--arch=aarch64", args)
        self.assertIn("--enable-cross-compile", args)
        self.assertIn("--enable-ohosavcodecdecoder", args)


class CliTest(unittest.TestCase):
    def test_build_args(self):
        args = build_parser().parse_args(
            ["build", "--platform", "windows", "--libs", "ffmpeg", "--abi", "x64", "-j", "8",
             "-o", "D:/out"])
        self.assertEqual(args.platform, "windows")
        self.assertEqual(args.libs, "ffmpeg")
        self.assertEqual(args.abi, ["x64"])
        self.assertEqual(args.jobs, 8)
        self.assertEqual(args.output, "D:/out")

    def test_output_root_is_mandatory(self):
        saved = os.environ.pop("FFMPEG_3RD_OUTPUT", None)
        try:
            with self.assertRaises(RuntimeError):
                resolve_output_root(None)
            self.assertEqual(resolve_output_root("D:/out"), Path("D:/out").resolve())
            # 不落盘的命令可以省略
            self.assertTrue(resolve_output_root(None, required=False).is_absolute())
        finally:
            if saved:
                os.environ["FFMPEG_3RD_OUTPUT"] = saved

    def test_build_without_output_fails(self):
        saved = os.environ.pop("FFMPEG_3RD_OUTPUT", None)
        try:
            args = build_parser().parse_args(["build", "--platform", "windows"])
            with self.assertRaises(RuntimeError):
                make_context(args, Logger(verbose=False))
        finally:
            if saved:
                os.environ["FFMPEG_3RD_OUTPUT"] = saved


if __name__ == "__main__":
    unittest.main()
