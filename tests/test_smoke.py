"""冒烟测试：配置齐全、配方与平台注册一致、CLI 可解析。

运行: ``python -m unittest discover -s tests -t .``
"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from ffmpeg_build import recipes
from ffmpeg_build.cli import build_parser, make_context
from ffmpeg_build.config import (BUILD_ORDER, DEPS_FILE, DEPS_LIBRARIES, LIBRARIES,
                                 OPTIONAL_LIBRARIES, PROJECT_ROOT, dep_group, dep_profile,
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
        # 版本/地址/commit 必须由 config/<profile>_deps.json 决定（可复现构建的唯一来源）
        self.assertEqual(LIBRARIES["spdlog"].version, DEPS_LIBRARIES["spdlog"]["version"])
        self.assertTrue(LIBRARIES["openssl"].source.url.startswith("http"))
        self.assertTrue(LIBRARIES["libyuv"].source.commit)   # git 源必须锁 commit

    def test_deps_groups_and_profile(self):
        # ffmpeg 与 ffmpeg 构建期依赖分开；ffmpeg 带 capture（开播）用途标记
        self.assertEqual(dep_group("ffmpeg"), "ffmpeg")
        self.assertEqual(dep_group("openssl"), "ffmpeg_deps")
        self.assertEqual(dep_group("spdlog"), "standalone")
        self.assertEqual(dep_profile("ffmpeg"), "capture")
        self.assertEqual(dep_profile("openssl"), "")

    def test_deps_file_location(self):
        # 锁定文件必须放在独立 config 目录，并按用途命名（capture 开播 / player 播放）
        self.assertTrue(DEPS_FILE.exists(), DEPS_FILE)
        self.assertEqual(DEPS_FILE, (PROJECT_ROOT / "config" / "capture_deps.json").resolve())

    def test_profile_whitelist(self):
        # 裁剪白名单按 profile 分开：开播要编码+封装+推流，播放只要解码+解封装
        from ffmpeg_build.config import (FFMPEG_DEP_FLAGS, PROFILE_WHITELIST, whitelist,
                                         whitelist_encoders)
        self.assertEqual(set(PROFILE_WHITELIST), {"capture", "player"})
        capture, player = whitelist("capture"), whitelist("player")
        self.assertIn("libopenh264", capture["encoders"])
        self.assertIn("flv", capture["muxers"])
        self.assertIn("rtmp", capture["protocols"])
        self.assertEqual(player["encoders"], [])          # 播放不编码 → 不生成 --enable-encoder
        self.assertEqual(player["muxers"], [])
        self.assertIn("hevc", player["decoders"])
        self.assertIn("mkv", player["demuxers"])
        # 开播的 ffmpeg 外部库开关来自锁定文件的 ffmpeg_deps 组（不再是硬编码）
        self.assertEqual(sorted(FFMPEG_DEP_FLAGS),
                         ["--enable-libfdk-aac", "--enable-libopenh264", "--enable-libsrt",
                          "--enable-openssl"])
        # 平台编码器 + nvenc 只按 profile 追加
        self.assertIn("h264_mediacodec", whitelist_encoders("android", False, "capture"))
        self.assertEqual(whitelist_encoders("android", False, "player"), [])
        self.assertIn("h264_nvenc", whitelist_encoders("windows", True, "capture"))
        # 未知 profile 回落到开播，避免拼错时静默编出半套组件
        self.assertIs(whitelist("unknown"), capture)

    def test_work_dir_has_profile(self):
        # 中间目录必须带 profile：capture 与 player 的 configure 开关不同，不能复用同一份中间产物
        from ffmpeg_build.config import BuildContext
        ctx = BuildContext(platform="windows", abis=["x64"], output_root="D:/out", profile="player")
        self.assertTrue(str(ctx.work_dir("ffmpeg", "x64")).replace("\\", "/")
                        .endswith("build/player-windows-x64/ffmpeg"))
        # 日志与下载缓存同样带 profile 标记；源码目录共用（同一 URL+commit 不重复克隆）
        self.assertTrue(str(ctx.log_root).replace("\\", "/").endswith("build/player-logs"))
        self.assertTrue(str(ctx.cache_root).replace("\\", "/").endswith(".cache/player"))
        self.assertTrue(str(ctx.source_dir("ffmpeg")).replace("\\", "/").endswith("sources/ffmpeg"))

    def test_player_deps_sample(self):
        # 播放链路示例：只有 ffmpeg + json + spdlog，且与开播共用同一个 ffmpeg 版本/commit
        import json
        data = json.loads((PROJECT_ROOT / "config" / "player_deps.json").read_text(encoding="utf-8"))
        self.assertEqual(data["profile"], "player")
        names = set(data["groups"]["ffmpeg"]["profiles"]["player"]["libraries"]) | \
            set(data["groups"]["standalone"]["libraries"])
        self.assertEqual(names, {"ffmpeg", "json", "spdlog"})
        ffmpeg = data["groups"]["ffmpeg"]["profiles"]["player"]["libraries"]["ffmpeg"]
        self.assertEqual(ffmpeg["version"], DEPS_LIBRARIES["ffmpeg"]["version"])
        self.assertEqual(ffmpeg["commit"], DEPS_LIBRARIES["ffmpeg"]["commit"])

    def test_deps_file_selectable(self):
        from ffmpeg_build import deps_env
        saved = {k: os.environ.get(k) for k in (deps_env.ENV_DEPS_FILE, deps_env.ENV_PROFILE,
                                                deps_env.ENV_CONFIG_DIR)}
        try:
            deps_env.apply_from_argv(["build", "--platform", "windows", "--config-dir", "D:/cfg"])
            self.assertEqual(deps_env.deps_file(), Path("D:/cfg/capture_deps.json"))
            deps_env.apply_from_argv(["build", "--platform", "windows", "--profile", "player"])
            self.assertEqual(deps_env.deps_file(), Path("D:/cfg/player_deps.json"))
            deps_env.apply_from_argv(["build", "--platform", "windows", "--deps", "D:/x/my.json"])
            self.assertEqual(deps_env.deps_file(), Path("D:/x/my.json"))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

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

    def test_build_marker_name(self):
        from datetime import datetime
        from ffmpeg_build.utils import build_marker_name
        name = build_marker_name("ffmpeg", "6.1.6", "windows", "x64", "msvc",
                                 "1a2b3c4", datetime(2026, 10, 19, 10, 32))
        self.assertEqual(name, "version-ffmpeg-6.1.6-g1a2b3c4-2026-10-19-10-32.windows.x64.msvc.txt")
        # 无 commit（归档源码）时省略 g<sha>；iOS 无 abi 层
        self.assertEqual(build_marker_name("libyuv", "0b9e5c92", "ios", "", "xcode-clang",
                                           "", datetime(2026, 10, 19, 11, 5)),
                         "version-libyuv-0b9e5c92-2026-10-19-11-05.ios.xcode-clang.txt")
        # ffmpeg 带 capture（开播）用途标记
        self.assertEqual(build_marker_name("ffmpeg", "6.1.6", "windows", "x64", "msvc",
                                           "3c1fc5df", datetime(2026, 10, 19, 10, 32), "capture"),
                         "version-ffmpeg-6.1.6-g3c1fc5df-2026-10-19-10-32.windows.x64.capture.msvc.txt")

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
