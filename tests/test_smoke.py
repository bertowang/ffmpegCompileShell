"""冒烟测试：配置齐全、配方与平台注册一致、CLI 可解析。

运行: ``python -m unittest discover -s tests -t .``
"""

import unittest

from ffmpeg_build.cli import build_parser
from ffmpeg_build.config import BUILD_ORDER, LIBRARIES, OPTIONAL_LIBRARIES, resolve_selection
from ffmpeg_build.recipes import REGISTRY
from ffmpeg_build.toolchains import TOOLCHAINS

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

    def test_selection_default_excludes_optional(self):
        self.assertEqual(resolve_selection(None), list(BUILD_ORDER))
        self.assertEqual(resolve_selection("json"), ["json"])
        self.assertEqual(resolve_selection("ffmpeg,srt"), ["ffmpeg", "srt"])
        with self.assertRaises(KeyError):
            resolve_selection("x264")


class PlatformTest(unittest.TestCase):
    def test_platforms(self):
        self.assertEqual(set(TOOLCHAINS), {"windows", "android", "ios"})


class CliTest(unittest.TestCase):
    def test_build_args(self):
        args = build_parser().parse_args(
            ["build", "--platform", "windows", "--libs", "ffmpeg", "--abi", "x64", "-j", "8"])
        self.assertEqual(args.platform, "windows")
        self.assertEqual(args.libs, "ffmpeg")
        self.assertEqual(args.abi, ["x64"])
        self.assertEqual(args.jobs, 8)


if __name__ == "__main__":
    unittest.main()
