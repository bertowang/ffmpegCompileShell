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
from ffmpeg_build.toolchains import (LINUX_ABI, OHOS_ABI, OHOSToolchain, LinuxToolchain,
                                     TOOLCHAINS, normalize_platform)
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
        # https（拉水印/logo）与 libsrt（SRT 协议的正确内部名；写 srt 会被 configure 静默忽略）
        self.assertIn("https", capture["protocols"])
        self.assertIn("libsrt", capture["protocols"])
        self.assertNotIn("srt", capture["protocols"])
        # 图片水印要经 avformat 打开，必须有 image2 类 demuxer
        self.assertIn("image2", capture["demuxers"])
        self.assertEqual(player["encoders"], [])          # 播放不编码 → 不生成 --enable-encoder
        self.assertEqual(player["muxers"], [])
        self.assertIn("hevc", player["decoders"])
        self.assertIn("mkv", player["demuxers"])
        # 开播的 ffmpeg 外部库开关来自锁定文件的 ffmpeg_deps 组（不再是硬编码）
        self.assertEqual(sorted(FFMPEG_DEP_FLAGS),
                         ["--enable-libfdk-aac", "--enable-libopenh264", "--enable-libsrt",
                          "--enable-openssl", "--enable-zlib"])
        # 平台编码器按平台取：Android 只有 mediacodec，不会带上桌面硬编
        self.assertIn("h264_mediacodec", whitelist_encoders("android", "capture"))
        self.assertEqual(whitelist_encoders("android", "player"), [])
        self.assertIn("h264_nvenc", whitelist_encoders("windows", "capture", nvenc=True))
        # 未知 profile 回落到开播，避免拼错时静默编出半套组件
        self.assertIs(whitelist("unknown"), capture)

    def test_hardware_encoders_are_windows_only(self):
        # 桌面独显硬编只在 Windows：移动端既没有 NVENC/AMF 的 SDK，也对体积极敏感，
        # 因此即便开了开关也不能串到 android/ios 上
        from ffmpeg_build.config import HARDWARE_ENCODERS, whitelist_encoders
        self.assertEqual(set(HARDWARE_ENCODERS), {"windows"})
        win = whitelist_encoders("windows", "capture", nvenc=True, amf=True)
        self.assertIn("h264_nvenc", win)
        self.assertIn("h264_amf", win)
        self.assertIn("libopenh264", win)          # 软编回退始终保留
        for platform in ("android", "ios", "ohos"):
            enc = whitelist_encoders(platform, "capture", nvenc=True, amf=True)
            self.assertNotIn("h264_nvenc", enc)
            self.assertNotIn("h264_amf", enc)
        # 没开开关时 Windows 也不带，保证默认构建不依赖 SDK
        base = whitelist_encoders("windows", "capture")
        self.assertNotIn("h264_nvenc", base)
        self.assertNotIn("h264_amf", base)
        # 播放器不编码
        self.assertEqual(whitelist_encoders("windows", "player", nvenc=True, amf=True), [])

    def test_hw_sdks_are_declared_per_platform(self):
        # 硬编 SDK 必须在锁定文件里声明，并用 platforms 限定平台：
        # windows 才有，ios/android 一律拿不到（移动端体积敏感 + 用不上）
        from ffmpeg_build.config import HW_SDKS, SDK_SOURCES, hw_sdk
        self.assertIn("amf", HW_SDKS)
        self.assertIn("nv-codec-headers", HW_SDKS)
        for name in HW_SDKS:
            self.assertEqual(HW_SDKS[name].get("platforms"), ["windows"], name)
            self.assertTrue(HW_SDKS[name]["url"].startswith("http"), name)
            self.assertTrue(HW_SDKS[name].get("commit"), name)   # 必须锁 commit
            self.assertIn(name, SDK_SOURCES)
            self.assertIsNotNone(hw_sdk(name, "windows"), name)
            for platform in ("android", "ios", "ohos"):
                self.assertIsNone(hw_sdk(name, platform), f"{name}@{platform}")
        # SDK 不是"要编译的库"，不能混进 --libs
        self.assertNotIn("amf", LIBRARIES)
        self.assertNotIn("nv-codec-headers", LIBRARIES)

    def test_hw_sdk_sources_are_pinned_and_sparse(self):
        # SDK 直接用上游仓库，但必须锁 tag：跟 master 会让版本漂移（AMF 已到 1.5.x、
        # nv-codec-headers 有 12.0.16.2），构建就不可复现了
        from ffmpeg_build.config import HW_SDKS, SDK_SOURCES
        for name in ("amf", "nv-codec-headers"):
            spec = HW_SDKS[name]
            self.assertTrue(spec["url"].startswith("https://github.com/"), name)
            self.assertTrue(spec.get("tag"), f"{name}: 必须锁 tag")
            self.assertFalse(spec.get("internal"), name)
            self.assertTrue(spec.get("commit"), f"{name}: 必须锁 commit")
        # AMF 仓库带 Thirdparty/（DeckLink SDK、预编译 ffmpeg-6.0 等约 490MB），
        # 只服务于它自己的 samples —— 全量 clone 是纯浪费，必须稀疏检出
        amf = HW_SDKS["amf"]
        src = SDK_SOURCES["amf"]
        self.assertEqual(tuple(amf.get("sparse") or ()), ("amf/public/include",))
        self.assertEqual(src.sparse, ("amf/public/include",))
        self.assertEqual(src.depth, 1)
        self.assertTrue(src.partial)
        # 稀疏路径必须覆盖 include_dir，否则头文件检不出来，要到 configure 才炸
        self.assertIn(amf["include_dir"], [p.strip("/") for p in amf["sparse"]])

    def test_build_dir_is_rebuilt_when_configure_args_change(self):
        # 保险丝：MSVC 下 ffmpeg 的 .d 依赖常是空文件，make 感知不到 config.h 变化，
        # 会出现"配置改了却只重编一部分"（编码器进了 DLL，exe 里的 configuration 串却是旧的）。
        # 故按 configure 命令行做指纹：变了就整个中间目录重建。
        import tempfile
        from types import SimpleNamespace
        from ffmpeg_build.recipes import _ensure_build_dir
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "ffmpeg"
            ctx = SimpleNamespace(logger=Logger(verbose=False))
            first = ["--prefix=/x", "--enable-encoder=h264_nvenc"]
            build = _ensure_build_dir(work, first, ctx)
            self.assertEqual(build, work / "build")
            stale = build / "ffmpeg.o"
            stale.write_text("old")

            # 参数没变 -> 增量，旧目标文件保留
            _ensure_build_dir(work, first, ctx)
            self.assertTrue(stale.is_file())

            # 参数变了 -> 全量，旧目标文件必须被清掉
            changed = first + ["--enable-amf"]
            _ensure_build_dir(work, changed, ctx)
            self.assertFalse(stale.exists())
            self.assertTrue((work / "build").is_dir())
            self.assertEqual((work / ".configure_args").read_text(encoding="utf-8"),
                             "\n".join(changed))

            # --dry-run 只打印将要执行的命令，绝不能顺手把中间目录删掉
            keep = work / "build" / "ffmpeg.o"
            keep.write_text("keep")
            _ensure_build_dir(work, ["--prefix=/other"],
                              SimpleNamespace(logger=Logger(verbose=False), dry_run=True))
            self.assertTrue(keep.is_file())

    def test_prepare_hw_sdk_install_layout(self):
        # SDK 头文件按官方 install 布局装进**自己的**库目录（<库>/<平台>/include）：
        # 上游没有 AMF/ 这一层，要规整出来；且绝不能装进 ffmpeg/ 里（会被整目录覆盖删掉）
        import tempfile
        from types import SimpleNamespace
        from ffmpeg_build.recipes import prepare_hw_sdk
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "amf" / "public" / "include" / "core").mkdir(parents=True)
            (src / "amf" / "public" / "include" / "core" / "Version.h").write_text("#define X 1")
            install = root / "out" / "amf" / "windows"

            def make_ctx(platform):
                return SimpleNamespace(
                    platform=platform, force_fetch=False, abis=["x64"],
                    logger=Logger(verbose=False),
                    source_dir=lambda name: src,
                    sources=SimpleNamespace(ensure=lambda *a, **k: src),
                    install_dir=lambda name, abi=None: root / "out" / name / platform,
                    toolchain=SimpleNamespace(layout_of=lambda n: "flat", marker_variant="crtmt"))

            out = prepare_hw_sdk(make_ctx("windows"), "amf", "x64")
            self.assertEqual(out, install / "include")
            self.assertTrue((install / "include" / "AMF" / "core" / "Version.h").is_file())
            # 上游源码保持原样，不做就地改写
            self.assertTrue((src / "amf" / "public" / "include" / "core" / "Version.h").is_file())
            # 版本标记与其他库同款
            self.assertEqual(len(list(install.glob("version-*.txt"))), 1)
            # 平台不匹配：直接报错，而不是悄悄编出没有硬编的版本
            with self.assertRaises(RuntimeError):
                prepare_hw_sdk(make_ctx("android"), "amf", "arm64-v8a")

    def test_auto_bsfs_always_enabled(self):
        # avformat 会自动插入 extract_extradata（flvenc 的 h264、demux 缺 extradata 时），
        # 裁掉只在运行期报 AVERROR_BSF_NOT_FOUND，configure 看不出来 → 强制并入所有 profile
        from ffmpeg_build.config import AUTO_BSFS, whitelist, whitelist_bsfs
        self.assertIn("extract_extradata", AUTO_BSFS)
        for profile in ("capture", "player"):
            bsfs = whitelist_bsfs(profile)
            for name in AUTO_BSFS:
                self.assertIn(name, bsfs)
            self.assertEqual(len(bsfs), len(set(bsfs)))       # 去重
            self.assertGreaterEqual(set(bsfs), set(whitelist(profile)["bsfs"]))

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
        # 用真实临时目录而不是 "D:/cfg"：后者在 POSIX 上会被当成相对路径解析到工程目录，
        # 导致测试只能在 Windows 上通过。
        import tempfile
        from ffmpeg_build import deps_env
        saved = {k: os.environ.get(k) for k in (deps_env.ENV_DEPS_FILE, deps_env.ENV_PROFILE,
                                                deps_env.ENV_CONFIG_DIR)}
        cfg = Path(tempfile.mkdtemp()).resolve()
        try:
            deps_env.apply_from_argv(["build", "--platform", "linux", "--config-dir", str(cfg)])
            self.assertEqual(deps_env.deps_file(), cfg / "capture_deps.json")
            deps_env.apply_from_argv(["build", "--platform", "linux", "--profile", "player"])
            self.assertEqual(deps_env.deps_file(), cfg / "player_deps.json")
            deps_env.apply_from_argv(["build", "--platform", "linux", "--deps", str(cfg / "my.json")])
            self.assertEqual(deps_env.deps_file(), cfg / "my.json")
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
        self.assertEqual(set(TOOLCHAINS), {"windows", "android", "ios", "ohos", "linux"})

    def test_aliases(self):
        self.assertEqual(normalize_platform("openharmony"), "ohos")
        self.assertEqual(normalize_platform("win64"), "windows")
        self.assertEqual(normalize_platform("linux64"), "linux")

    def test_every_platform_has_layout_and_default(self):
        # 新增平台时最容易漏的两张表：PLATFORM_LAYOUT 与 DEFAULT_PLATFORM_LAYOUT。
        # 漏了会让 install_dir 静默回落到 "abi"，产物目录与文档不一致。
        from ffmpeg_build.config import DEFAULT_PLATFORM_LAYOUT, PLATFORM_LAYOUT
        self.assertEqual(set(PLATFORM_LAYOUT), set(TOOLCHAINS))
        self.assertEqual(set(DEFAULT_PLATFORM_LAYOUT), set(TOOLCHAINS))


class LinuxTest(unittest.TestCase):
    """Linux 工具链：本机 gcc/clang + GNU make，验证路径/环境/参数拼装。"""

    def _context(self):
        ctx = SimpleNamespace(logger=Logger(verbose=False), runner=None,
                              abis=["x86_64"], output_root=Path("out"), platform="linux")
        tc = LinuxToolchain(ctx)
        ctx.toolchain = tc
        return ctx, tc

    def test_abi_table(self):
        self.assertEqual(set(LINUX_ABI), {"x86_64", "arm64", "armhf"})

    def test_build_env(self):
        _, tc = self._context()
        env = tc.build_env("x86_64")
        self.assertEqual(env["CC"], tc.cc)
        self.assertIn("-fPIC", env["CFLAGS"])

    def test_cmake_args_and_install_dir(self):
        _, tc = self._context()
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", tc.cmake_args("x86_64"))
        self.assertEqual(tc.install_dir("ffmpeg", "x86_64"),
                         Path("out") / "ffmpeg" / "linux" / "x86_64")

    def test_expand_abis(self):
        _, tc = self._context()
        self.assertEqual(tc.expand_abis(None), tc.default_abis)   # 默认取宿主架构
        self.assertEqual(tc.expand_abis(["all"]), ["x86_64", "arm64", "armhf"])

    def test_ffmpeg_native_args(self):
        # 本机 Linux 不做交叉编译：不能出现 --enable-cross-compile / --sysroot / xcrun
        from ffmpeg_build import recipes
        ctx, tc = self._context()
        tc.nasm = "/usr/bin/nasm"
        args = recipes._ffmpeg_platform_args(ctx, "x86_64")
        joined = " ".join(args)
        self.assertIn("--target-os=linux", args)
        self.assertIn("--arch=x86_64", args)
        self.assertIn("--enable-static", args)
        self.assertNotIn("--enable-cross-compile", args)
        self.assertNotIn("xcrun", joined)
        # 没有 nasm 时降级关闭 x86 汇编，而不是编译到一半才失败
        tc.nasm = ""
        self.assertIn("--disable-x86asm", recipes._ffmpeg_platform_args(ctx, "x86_64"))


class PlatformOverrideTest(unittest.TestCase):
    """platform_overrides：同一份锁定文件按平台换源码来源。"""

    def test_override_only_for_matching_platform(self):
        from ffmpeg_build.config import dep_source
        ffmpeg = dep_source("ffmpeg")
        self.assertTrue(ffmpeg.url.startswith("https://git.woa.com"))     # 默认仍是内网
        linux = dep_source("ffmpeg", "linux")
        self.assertNotEqual(linux.url, ffmpeg.url)
        self.assertTrue(linux.commit)                                     # 覆盖项也要锁 commit
        self.assertEqual(dep_source("ffmpeg", "windows").url, ffmpeg.url)  # 未命中沿用原条目

    def test_override_is_not_ffmpeg_only(self):
        # fdk-aac 在 Linux 上换上游镜像（commit 与内网版相同），证明这是通用能力
        from ffmpeg_build.config import dep_source
        default = dep_source("fdk-aac")
        linux = dep_source("fdk-aac", "linux")
        self.assertTrue(default.url.startswith("https://git.woa.com"))
        self.assertIn("github.com", linux.url)
        self.assertEqual(linux.commit, default.commit)   # 同一份源码，只换下载地址


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

    def test_crt_option(self):
        # 默认 /MT 与现有产物一致；--crt md 给 /MD 的消费方（只影响 CMake 构建的库）
        from ffmpeg_build.config import DEFAULT_CRT
        from ffmpeg_build.toolchains import WindowsToolchain
        args = build_parser().parse_args(["build", "--platform", "windows", "--crt", "md",
                                          "-o", "D:/out"])
        self.assertEqual(args.crt, "md")
        self.assertEqual(DEFAULT_CRT, "mt")
        for crt, lib, flag in (("mt", "MultiThreaded", "/MT"), ("md", "MultiThreadedDLL", "/MD")):
            ctx = SimpleNamespace(abis=["x64"], output_root=Path("out"), crt=crt,
                                  logger=Logger(verbose=False), runner=None)
            tc = WindowsToolchain(ctx)
            tc.msvc = SimpleNamespace(env={}, generator=None)
            cmake = tc.cmake_args("x64")
            self.assertIn(f"-DCMAKE_MSVC_RUNTIME_LIBRARY={lib}", cmake)
            self.assertIn(f"-DCMAKE_CXX_FLAGS_RELEASE={flag} /O2 /Ob2 /DNDEBUG", cmake)
            self.assertEqual(tc.crt_flag, flag)

    def test_windows_layout_matches_3rd(self):
        # Windows 产物布局要能整目录拷进 MediaX/ESMediaX/3rd 直接替换
        from ffmpeg_build.config import WINDOWS_LAYOUT, platform_layout, windows_layout
        from ffmpeg_build.toolchains import IOSToolchain, WindowsToolchain
        self.assertEqual(windows_layout("ffmpeg"), "flat")     # ffmpeg/windows/{include,lib,bin}
        self.assertEqual(windows_layout("libyuv"), "flat")
        self.assertEqual(windows_layout("spdlog"), "lib")      # spdlog/windows/lib/<abi>
        self.assertEqual(windows_layout("unknown_lib"), "abi")  # 未登记的默认带 abi 层

        ctx = SimpleNamespace(abis=["x64"], output_root=Path("D:/out"), crt="mt",
                              logger=Logger(verbose=False), runner=None)
        tc = WindowsToolchain(ctx)
        self.assertEqual(tc.install_dir("ffmpeg", "x64"), Path("D:/out/ffmpeg/windows"))
        self.assertEqual(tc.install_dir("spdlog", "x64"), Path("D:/out/spdlog/windows"))
        # 未登记的库保持 abi 层
        self.assertEqual(tc.install_dir("unknown_lib", "x64"), Path("D:/out/unknown_lib/windows/x64"))
        # 多 ABI 同时构建时 flat/lib 会互相覆盖 → 自动回落到 abi 目录
        multi = SimpleNamespace(abis=["x64", "x86"], output_root=Path("D:/out"), crt="mt",
                                logger=Logger(verbose=False), runner=None)
        tc2 = WindowsToolchain(multi)
        self.assertEqual(tc2.install_dir("ffmpeg", "x64"), Path("D:/out/ffmpeg/windows/x64"))
        self.assertIn("ffmpeg", WINDOWS_LAYOUT)
        # Android / OHOS：库带 abi 层，但 spdlog 与 3rd-bak 一致是不带 abi 的
        self.assertEqual(platform_layout("android", "ffmpeg"), "abi")
        self.assertEqual(platform_layout("android", "spdlog"), "flat")
        self.assertEqual(platform_layout("ohos", "spdlog"), "flat")
        self.assertEqual(platform_layout("ios", "ffmpeg"), "flat")   # fat 库无 abi 层
        # iOS：多架构仍然合并为 fat（不回落 abi 目录）
        ios_ctx = SimpleNamespace(abis=["arm64", "x86_64"], output_root=Path("D:/out"), crt="mt",
                                  logger=Logger(verbose=False), runner=None)
        ios = IOSToolchain(ios_ctx, min_version="13.0")
        self.assertEqual(ios.install_dir("ffmpeg"), Path("D:/out/ffmpeg/ios"))
        self.assertEqual(ios.marker_variant, "min13.0")

    def test_portable_layout(self):
        # header-only（json）与平台/ABI 无关：不带 <平台>/<abi> 层，三端共用一份
        from ffmpeg_build.config import is_portable
        from ffmpeg_build.toolchains import WindowsToolchain
        self.assertTrue(is_portable("json"))
        self.assertFalse(is_portable("ffmpeg"))
        self.assertFalse(is_portable("spdlog"))
        # 只输出 single_include（真正的合并单头），不再给它一份 include 副本
        self.assertEqual(DEPS_LIBRARIES["json"].get("output_subdirs"), ["single_include"])
        ctx = SimpleNamespace(abis=["x64"], output_root=Path("D:/out"), crt="mt",
                              logger=Logger(verbose=False), runner=None)
        tc = WindowsToolchain(ctx)
        self.assertEqual(tc.install_dir("json", "x64"), Path("D:/out/json"))
        # 标记里 platform=any、无 abi、compiler=header-only
        from datetime import datetime
        from ffmpeg_build.utils import build_marker_name
        self.assertEqual(build_marker_name("json", "3.12.0", "any", "", "header-only",
                                           "", datetime(2026, 10, 20, 11, 25)),
                         "version-json-3.12.0-2026-10-20-11-25.any.header-only.txt")

    def test_move_lib_into_abi(self):
        import tempfile
        from ffmpeg_build.toolchains import WindowsToolchain
        root = Path(tempfile.mkdtemp())
        (root / "lib").mkdir()
        (root / "lib" / "spdlog.lib").write_text("x")
        (root / "lib" / "cmake").mkdir()
        WindowsToolchain._move_lib_into_abi(root, "x64")           # 幂等：再调一次不应嵌套
        WindowsToolchain._move_lib_into_abi(root, "x64")
        self.assertTrue((root / "lib" / "x64" / "spdlog.lib").exists())
        self.assertFalse((root / "lib" / "x64" / "x64").exists())

    def test_build_marker_name(self):
        from datetime import datetime
        from ffmpeg_build.utils import build_marker_name
        name = build_marker_name("ffmpeg", "6.1.6", "windows", "x64", "msvc",
                                 "1a2b3c4", datetime(2026, 10, 19, 10, 32), variant="crtmt")
        self.assertEqual(name, "version-x64-ffmpeg-6.1.6-g1a2b3c4-2026-10-19-10-32"
                               ".windows.x64.msvc.crtmt.txt")
        # 无 commit（归档源码）时省略 g<sha>；iOS 无 abi 层（文件名里也就没有 abi 前缀）
        self.assertEqual(build_marker_name("libyuv", "0b9e5c92", "ios", "", "xcode-clang",
                                           "", datetime(2026, 10, 19, 11, 5)),
                         "version-libyuv-0b9e5c92-2026-10-19-11-05.ios.xcode-clang.txt")
        # ffmpeg 带 capture（开播）用途标记
        self.assertEqual(build_marker_name("ffmpeg", "6.1.6", "windows", "x64", "msvc",
                                           "3c1fc5df", datetime(2026, 10, 19, 10, 32), "capture"),
                         "version-x64-ffmpeg-6.1.6-g3c1fc5df-2026-10-19-10-32"
                         ".windows.x64.capture.msvc.txt")
        # x86 也要能一眼分清
        self.assertEqual(build_marker_name("spdlog", "v1.14.1", "windows", "x86", "msvc",
                                           "27cb4c7", datetime(2026, 10, 20, 9, 5), variant="crtmd"),
                         "version-x86-spdlog-v1.14.1-g27cb4c7-2026-10-20-09-05"
                         ".windows.x86.msvc.crtmd.txt")
        # Android / iOS 的关键信息：API 级别与最低系统版本（3rd-bak 里的文件名也是这么记的）
        self.assertEqual(build_marker_name("ffmpeg", "6.1.6", "android", "arm64-v8a", "ndk-clang",
                                           "3c1fc5df", datetime(2026, 10, 20, 12, 10),
                                           "capture", "api21"),
                         "version-arm64-v8a-ffmpeg-6.1.6-g3c1fc5df-2026-10-20-12-10"
                         ".android.arm64-v8a.capture.ndk-clang.api21.txt")
        self.assertEqual(build_marker_name("ffmpeg", "6.1.6", "ios", "", "xcode-clang",
                                           "3c1fc5df", datetime(2026, 10, 20, 12, 10),
                                           "capture", "min13.0"),
                         "version-ffmpeg-6.1.6-g3c1fc5df-2026-10-20-12-10"
                         ".ios.capture.xcode-clang.min13.0.txt")

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


class FfmpegRecipeTest(unittest.TestCase):
    def test_prune_source_tree_nogit(self):
        # 非 git 源码树：删掉已知生成文件，且**不能**动 ffbuild/ 里的 tracked 源码
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "libavfilter").mkdir()
            (src / "ffbuild").mkdir()
            for rel in ("config.h", ".deps_pin", "libavfilter/filter_list.c",
                        "libavfilter/allfilters.c", "libavfilter/af_volume.o",
                        "ffbuild/common.mak", "ffbuild/config.mak"):
                (src / rel).write_text("x")
            self.assertTrue(recipes.prune_source_tree(src, Logger(verbose=False)))
            self.assertFalse((src / "config.h").exists())
            self.assertFalse((src / "libavfilter" / "filter_list.c").exists())
            self.assertFalse((src / "ffbuild" / "config.mak").exists())
            # 保留：源码管理器的 .deps_pin、tracked 源码（含 ffbuild 目录本体）
            self.assertTrue((src / ".deps_pin").exists())
            self.assertTrue((src / "libavfilter" / "allfilters.c").exists())
            self.assertTrue((src / "ffbuild" / "common.mak").exists())
            self.assertFalse(recipes.prune_source_tree(src, Logger(verbose=False)))

    def test_prune_source_tree_git(self):
        # git 源码树走 git clean -xfd：残留被清掉，tracked 源码与 .deps_pin 不受影响
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "libavfilter").mkdir()
            (src / "libavfilter" / "allfilters.c").write_text("x")
            (src / "ffbuild").mkdir()
            (src / "ffbuild" / "common.mak").write_text("x")
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "NUL", "GIT_CONFIG_SYSTEM": "NUL"}
            run = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True,
                                            capture_output=True, env=env)
            run("init", "-q")
            run("add", ".")
            run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
            # 模拟历史 in-source 构建残留
            (src / "config.h").write_text("x")
            (src / "libavfilter" / "filter_list.c").write_text("x")
            (src / "libavfilter" / "af_volume.o").write_text("x")
            (src / ".deps_pin").write_text("x")
            self.assertTrue(recipes.prune_source_tree(src, Logger(verbose=False)))
            self.assertFalse((src / "config.h").exists())
            self.assertFalse((src / "libavfilter" / "filter_list.c").exists())
            self.assertFalse((src / "libavfilter" / "af_volume.o").exists())
            self.assertTrue((src / ".deps_pin").exists())          # 锁定标记必须留
            self.assertTrue((src / "libavfilter" / "allfilters.c").exists())
            self.assertTrue((src / "ffbuild" / "common.mak").exists())
            self.assertFalse(recipes.prune_source_tree(src, Logger(verbose=False)))


if __name__ == "__main__":
    unittest.main()
