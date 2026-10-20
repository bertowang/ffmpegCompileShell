# FfmpegCompileShell

编译 **ffmpeg 及其依赖库** 的跨平台脚本（Python 3.8+，仅用标准库，零第三方依赖）。

## 目录

- [1. 依赖库清单](#1-依赖库清单)
- [2. 产物输出目录（必填）](#2-产物输出目录必填)
- [3. 使用攻略](#3-使用攻略)
  - [3.1 Windows](#31-首次使用windows)
  - [3.2 iOS / Android](#32-macos-上编译-ios--android)
  - [3.3 OHOS](#33-ohosopenharmony)
  - [3.4 分步 / 增量](#34-分步--增量)
  - [3.5 常用参数](#35-常用参数)
- [4. 目录结构](#4-目录结构脚本入库源码产物不入库)
- [5. ffmpeg 裁剪策略](#5-ffmpeg-裁剪策略)
- [6. 环境要求与排错](#6-环境要求与排错)
  - [6.1 Windows CRT /MT](#61-windows-crt-必须一致mt)
- [7. 与 MediaX 工程对接](#7-与-mediax-工程对接)
  - [7.1 cuda-samples（NPP）：现状与建议](#71-cuda-samplesnpp现状与将来建议)

> ⚠️ **产物输出目录必须显式指定**（`-o/--output` 或环境变量 `FFMPEG_3RD_OUTPUT`），
> 没有隐式默认值；漏给会直接报错退出，避免产物落到预期外的位置。详见第 2 节。

## 1. 依赖库清单

只保留开播（采集 → 编码 → 封装 → 推流）链路真正必须的库：

| 库 | 版本 | 作用 | 为什么必须 |
| - | - | - | - |
| openssl | 3.2.4 | TLS / RTMPS | ffmpeg `--enable-openssl`，推流链路加密 |
| srt | v1.5.4 | SRT 推流 | ffmpeg `--enable-libsrt` |
| fdk-aac | 2.0.3 | AAC 音频软编码 | ffmpeg `--enable-libfdk-aac` |
| openh264 | 2.3.1 | H.264 视频软编码 | ffmpeg `--enable-libopenh264` |
| libyuv | 0b9e5c92 | 格式转换 / 缩放 | 开播工程直接链接（不进 ffmpeg） |
| ffmpeg | 6.1.6 | 封装 + 协议 | muxer(flv/mpegts)、protocol(rtmp/rtmps/srt) |

可选（默认不构建）：

| 库 | 版本 | 说明 |
| - | - | - |
| json | 3.11.3 | nlohmann/json，header-only；MediaX 侧通常已由 `ESMediaX/3rd/json` submodule 提供，需要时 `--libs json` |
| spdlog | v1.14.1 | C++ 日志库（编译型静态库）。只有 **MediaX** 需要：其 `ESLogger` 基于 spdlog 实现（android_sink / msvc_sink / OHOS hilog callback_sink + Windows Release 异步日志）；TGCapture 自带 `core/Logger.h`，不链。需要时 `--libs spdlog` |

不使用 x264 / x265 / v265；AMF、nv-codec-headers、zlib、cuda-samples(NPP) 经复核均非开播编译期依赖，故不纳入。

### 1.1 依赖锁定文件 `config/capture_deps.json`

所有依赖的 **地址 / 版本 / commit** 统一锁在独立的 `config/` 目录里，按**用途**分文件命名：

| 文件 | 用途 | 状态 |
| - | - | - |
| `config/capture_deps.json` | **开播**（capture） | 当前的正式配置，默认加载 |
| `config/player_deps.json` | **播放**（player） | 示例骨架：`ffmpeg 6.1.6` + `json` + `spdlog`（不含 openssl/srt/fdk-aac/openh264/libyuv） |

两份文件互不干扰：`config.py` 只加载本次指定的那一份，**锁定文件里没列的库不会进入构建集合**
（所以 player 下 `python build.py list` 只会看到 ffmpeg / json / spdlog）。

**只要确定这个文件，任何时候都能编出完全相同的目标库**，`config.py` 只保留用途描述。

编译时显式指定用哪一份（默认即 `config/capture_deps.json`）：

```bash
python build.py build --platform windows -o D:/work/TGCapture3rd                      # 默认 capture
python build.py build --platform windows --profile player  ...                         # config/player_deps.json
python build.py build --platform windows --config-dir D:/work/mycfg ...                # 换配置目录
python build.py build --platform windows --deps D:/work/capture_deps.json ...          # 直接给文件
# 等价环境变量：FFMPEG_3RD_PROFILE / FFMPEG_3RD_CONFIG_DIR / FFMPEG_3RD_DEPS
```

所有子命令（`list` / `bootstrap` / `fetch` / `build` / `clean`）都支持这三个参数；
`list` 与 `build` 开头会打印本次实际加载的锁定文件路径与 profile，便于回溯产物来源。

```jsonc
"openssl": { "kind": "archive", "version": "3.6.5",
             "url": "https://github.com/openssl/openssl/releases/download/openssl-3.6.5/openssl-3.6.5.tar.gz",
             "tag": "openssl-3.6.5", "commit": "c8bd5a57108599ac650bbae77fcabe3109dab2e8" },
"libyuv":  { "kind": "git", "version": "1974", "url": "https://chromium.googlesource.com/libyuv/libyuv",
             "ref": "2404f9730e2e48946d29c6d28e3b630302a8d842", "commit": "2404f9730e2e48946d29c6d28e3b630302a8d842" },
"ffmpeg":  { "kind": "git", "version": "6.1.6",
             "url": "https://git.woa.com/EST/media/e-ffmpeg/ffmpeg-6.1.6.git",   // 唯一使用内网的库
             "commit": "3c1fc5dfaec5687d9c418e119f1dadd7a97553b1" }
```

分三组（与 ffmpeg 的耦合关系）：

| 组 | 含义 | 库 |
| - | - | - |
| `ffmpeg` | FFmpeg 自身源码，按**用途(profile)**再分档 | `ffmpeg`（capture 文件里 profile=**capture**；player 文件里 profile=**player**） |
| `ffmpeg_deps` | FFmpeg 的**构建期依赖**，需对应 `--enable-*`，产物静态并入 avcodec/avformat | `openssl`、`srt`、`fdk-aac`、`openh264` |
| `standalone` | 与 FFmpeg **无关**的独立库，应用直接链接 | `libyuv`、`json`、`spdlog` |

规则：

* **内网仓库**（`EST/media/e-ffmpeg`）：`ffmpeg`（ffmpeg-6.1.6）、`fdk-aac`（fdk_aac_v2.0.3）。
* 其余库用**上游官方仓库**，取锁定日（`updated` 字段）的最新正式发布版；
  个别库为兼容 MediaX 会锁在旧版（如 `spdlog` 锁 v1.14.1，条目里有 `note` 说明原因）。
* ffmpeg 的 `capture`（开播）标记会写进版本文件名，与将来的 `player` 产物区分：

  ```
  version-ffmpeg-6.1.6-g3c1fc5df-2026-10-19-10-32.windows.x64.capture.msvc.txt
  version-spdlog-v1.14.1-g27cb4c767-2026-10-19-20-36.windows.x64.msvc.txt
  ```
* git 类依赖一律锁到 **commit**（不是 tag），归档类锁到 **release tag**（可选填 `sha256`）。
* 源码目录会写入 `.deps_pin`；若锁定文件改了版本，下次构建会**自动重新拉取**，
  不会误用旧源码（这是可复现的关键）。

升级某个库，只改 `config/capture_deps.json` 对应条目即可，无需动 `config.py`。

### 1.2 profile：开播(capture) 与 播放(player)

`profile = 用途`。它由 `--profile` 决定，并同时驱动四件事，两项用途互不干扰：

| 维度 | capture（开播） | player（播放） |
| - | - | - |
| 锁定文件 | `config/capture_deps.json` | `config/player_deps.json` |
| 参与构建的库 | 8 个（含 openssl/srt/fdk-aac/openh264/libyuv） | 3 个（ffmpeg / json / spdlog） |
| ffmpeg 外部库开关 | `--enable-openssl --enable-libsrt --enable-libfdk-aac --enable-libopenh264`（**来自锁定文件 `ffmpeg_deps` 组的 `ffmpeg_flag`**，非硬编码） | 无（ffmpeg_deps 组为空 → 不带任何 `--enable-*`，也不用先编这些库） |
| 裁剪白名单（`config.PROFILE_WHITELIST`） | 编码器 `libopenh264,libfdk_aac,aac`（+ 平台硬编 / nvenc）、封装 `flv,mpegts`、协议 `file,tcp,tls,rtmp,rtmps,srt` | **不生成** `--enable-encoder` / `--enable-muxer`；解码器 `h264,hevc,aac,mp3,png,mjpeg`、解封装 `mov,mp4,flv,mkv,mpegts,hls`、协议 `file,http,tcp,hls` |

```bash
python build.py list --profile player                          # 只看播放链路的库与白名单
python build.py build --platform windows --profile player -o D:/work/3rd-player --libs ffmpeg
```

隔离措施（避免两项用途互相污染）：

* **中间临时文件全部带 profile 标记**，便于区分与对照：
  `build/<profile>-<platform>-<abi>/<lib>/`（编译中间产物 + `install` 暂存）、
  `build/<profile>-logs/`（构建日志）、`.cache/<profile>/`（下载包）。
  capture 与 player 的 `configure` 开关不同，复用同一目录会串用旧的 `config.h`/目标文件。
  源码目录 `sources/<lib>` 两个 profile 共用（同一 URL+commit 不重复克隆；锁定值变了会自动重新拉取）。
* **产物目录**不区分 profile（`<lib>/<platform>/<abi>`），所以不同 profile **必须用不同 `-o`**；
  `build` 会在输出根写 `.build_profile` 记录归属，若与当前 profile 不一致直接报错并提示换目录。
* ffmpeg 的版本标记文件名带 profile：`version-ffmpeg-6.1.6-g…-…-…windows.x64.player.msvc.txt`。

补充：player 目前没有 openssl，因此协议只开到 `http`/`hls`；
若播放器需要 https/tls，把 `openssl` 加进 `config/player_deps.json` 的 `ffmpeg_deps` 组即可，
`--enable-openssl` 会由 `ffmpeg_flag` 自动带上。

## 2. 产物输出目录（必填）

`build` / `clean` 这类会往磁盘写产物的命令**必须**给出输出目录，二选一：

```bash
# 1) 命令行（推荐，最直观）
python build.py build --platform windows -o D:/work/TGCapture3rd

# 2) 环境变量（适合 CI）
export FFMPEG_3RD_OUTPUT=D:/work/TGCapture3rd      # Linux/macOS
setx FFMPEG_3RD_OUTPUT D:\work\TGCapture3rd        # Windows
```

命令行优先于环境变量。**没给就报错**，不会落到"工程同级目录"这类猜出来的位置：

```
ERROR 未指定产物输出目录，必须显式给出，避免产物落到预期外的位置。
  方式一：-o/--output <目录>      例: python build.py build --platform windows -o D:/work/TGCapture3rd
  方式二：export FFMPEG_3RD_OUTPUT=<目录>（Windows: setx FFMPEG_3RD_OUTPUT <目录>）
```

不需要输出目录的命令（`list` / `bootstrap` / `fetch`）可省略。

产物布局（与 `MediaX/ESMediaX/3rd/<库>/<平台>/<abi>` 一致，可直接替换）：

```
TGCapture3rd/
├── openssl/windows/x64/{include,lib,bin}
├── srt/windows/x64/{include,lib}
├── fdk-aac/windows/x64/{include,lib}
├── openh264/windows/x64/{include,lib}
├── libyuv/windows/x64/{include,lib}
├── ffmpeg/windows/x64/{include,lib,bin}
├── */android/arm64-v8a/...
└── */ios/...                       # iOS 为 fat 库，不带 abi 层
```

每个库目录下会生成一个**带完整信息的版本标记文件**，便于追溯：

```
version-<库>-<版本>-g<commit>-<构建时间>.<平台>[.<abi>].<编译器>.txt

version-ffmpeg-6.1.6-g1a2b3c4-2026-10-19-10-32.windows.x64.msvc.txt
version-spdlog-v1.14.1-g9cca280a4-2026-10-19-11-05.android.arm64-v8a.ndk-clang.txt
version-libyuv-0b9e5c92-2026-10-19-11-05.ios.xcode-clang.txt       # 归档源码无 commit、iOS 无 abi
```

* `-g<commit>`：源码目录的短 commit（7 位）；源码是压缩包（openssl/srt/fdk-aac/openh264/json）时省略
* 编译器标识：`msvc` / `mingw` / `ndk-clang` / `xcode-clang` / `ohos-clang`
* 文件内容同时记录 版本、commit、平台与 ABI、编译器、构建时间、构建主机、源码路径

## 3. 使用攻略

### 3.1 首次使用（Windows）

```powershell
# ① 检查并补齐工具链（缺 MSVC / 原生 Perl 会自动下载；winget 可用时自动静默安装）
python build.py bootstrap --platform windows --install

# ②（可选）只拉源码，确认内网/外网可达
python build.py fetch --platform windows

# ③ 编译全部依赖 -> D:/work/TGCapture3rd（目录必填）
python build.py build --platform windows -o D:/work/TGCapture3rd

# ④ 查看产物
python build.py list
dir D:\work\TGCapture3rd
```

一键脚本：`powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1`

### 3.2 macOS 上编译 iOS / Android

```bash
python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios -o D:/work/TGCapture3rd          # arm64 + x86_64 -> lipo 合并

python3 build.py bootstrap --platform android --install
python3 build.py build --platform android --abi arm64-v8a --api 21 -o D:/work/TGCapture3rd
```

脚本：`./scripts/build_ios.sh`、`./scripts/build_android.sh`

### 3.3 OHOS（OpenHarmony）

原 `MediaX/ESMediaX/3rd/build_*.py` 的能力已并入这里，统一用一套脚本产出：

```bash
# ① 指向 OHOS SDK（也可 --ohos-sdk 显式指定）
export OHOS_SDK=~/command-line-tools/sdk/default/openharmony

# ② 自检（SDK / cmake / make / perl / 宿主）
python build.py bootstrap --platform ohos

# ③ 编译（产物 -> <输出目录>/<库>/ohos/arm64-v8a）
python build.py build --platform ohos -o D:/work/TGCapture3rd            # 默认 arm64-v8a
python build.py build --platform ohos --abi all -o D:/work/TGCapture3rd  # 三个 ABI 全编
python build.py build --platform ohos --libs openh264 -o D:/work/TGCapture3rd
```

说明：

* ABI：`arm64-v8a` / `armeabi-v7a` / `x86_64`，默认 `arm64-v8a`。
* cmake 优先用 SDK 自带的 `native/build-tools/cmake/bin/cmake`，并注入
  `-DOHOS_PLATFORM=OHOS -DOHOS_ARCH=<abi> -DCMAKE_TOOLCHAIN_FILE=<sdk>/native/build/cmake/ohos.toolchain.cmake`。
* openssl 走 `linux-aarch64` / `linux-armv4` / `linux-x86_64` target；openh264 走 `make OS=linux ARCH=<arch>`。
* ffmpeg 默认改用 openharmony-tpc 的 fork（`ohos-n6.1.2_dev`），并追加 `--enable-ohosavcodecdecoder`；
  若改用官方 ffmpeg 源码，把 `config.py` 的 `OHOS_FFMPEG_EXTRA_ARGS` 置空即可。
* 宿主建议 Linux/macOS；Windows 需自备 MSYS2/Git Bash（提供 bash + GNU make + perl）。

### 3.4 分步 / 增量

```bash
set OUT=D:/work/TGCapture3rd
python build.py build --platform windows --libs openssl -o %OUT%   # 只编一个库
python build.py build --platform windows --libs spdlog  -o %OUT%   # 可选库：MediaX 的日志实现
python build.py build --platform windows --libs ffmpeg   -o %OUT%  # 依赖已就绪时单独重编 ffmpeg
python build.py build --platform windows --clean         -o %OUT%  # 清理后全量重建
python build.py clean --platform windows --all           -o %OUT%  # 只清理（含产物）
python build.py build --platform windows -j 8            -o %OUT%  # 指定并发
```

> ffmpeg 构建前会校验 `openssl / fdk-aac / openh264 / srt` 是否已产出，
> 未就绪会直接提示"请先编译依赖"，而不是报含糊的 `xxx not found`。

### 3.5 常用参数

| 参数 | 说明 | 默认 |
| - | - | - |
| `-p/--platform` | `windows` / `android` / `ios` | 必填 |
| `--abi` | windows `x64`；android `arm64-v8a`/`armeabi-v7a`/`x86_64`；ios `arm64`/`x86_64`/`fat` | 平台默认 |
| `--libs` | 逗号分隔，`all` 为必须库全集 | `all` |
| `-o/--output` | 产物输出目录 | **必填**（或环境变量 `FFMPEG_3RD_OUTPUT`） |
| `-j/--jobs` | 并行任务数 | CPU-1 |
| `--toolchain` | Windows 工具链偏好 `auto`/`msvc`/`mingw` | `auto`（优先 MSVC） |
| `--ffmpeg-ref` | ffmpeg 仓库分支/标签（不给则用 `config.py` 中的 ref） | 平台默认 |
| `--enable-nvenc` | Windows 启用 NVENC（需自备 nv-codec-headers） | 关 |
| `--lto` | ffmpeg 开启 LTO（MSVC 下易踩坑） | 关 |
| `--api` / `--min-version` | Android API / iOS 最低版本 | 21 / 12.0 |
| `--ohos-sdk` | OHOS SDK 根目录（含 `native/`），不给则取 `$OHOS_SDK` | `$OHOS_SDK` |
| `--force-fetch` | 强制重新拉取源码 | 关 |
| `--dry-run` | 只打印命令不执行 | 关 |

## 4. 目录结构（脚本入库，源码/产物不入库）

```
FfmpegCompileShell/
├── build.py                 CLI 入口
├── ffmpeg_build/
│   ├── config.py            依赖清单、ffmpeg 裁剪白名单、路径、BuildContext
│   ├── utils.py             日志 / 命令执行 / 下载解压 / 源码获取 / 文件工具
│   ├── toolchains.py        Windows(MSVC) / Android(NDK) / iOS(Xcode) / OHOS(OpenHarmony SDK)
│   ├── recipes.py           各库编译配方（一库一函数）
│   ├── bootstrap.py         工具链自检与自动补齐
│   └── cli.py               list / bootstrap / fetch / build / clean
├── scripts/                 各平台一键入口
└── tests/                   冒烟测试（python -m unittest discover -s tests -t .）
```

`sources/`、`build/`、`.cache/`、`tools/` 均在 `.gitignore` 中 —— **提交时只有脚本**。

扩展方式：
* 加库 → `config.py` 的 `LIBRARIES`/`BUILD_ORDER` + `recipes.py` 加 `build_xxx(ctx, abi)` 并在 `REGISTRY` 注册
* 加平台 → `toolchains.py` 继承 `Toolchain` 并在 `TOOLCHAINS` 注册

## 5. ffmpeg 裁剪策略

开播是窄链路，采用 `--disable-everything` + 白名单，而非全量：

* 只 enable：encoder `libopenh264,libfdk_aac,aac`（Android 加 `h264_mediacodec`，iOS 加 `h264_videotoolbox`）、
  decoder `h264,aac,png`、muxer `flv,mpegts`、demuxer `mov,flv,mp4`、parser `h264,aac`、
  bsf `h264_mp4toannexb,aac_adtstoasc,null`、filter `scale,format,aformat,aresample,overlay,null,anull`、
  protocol `file,tcp,tls,rtmp,rtmps,srt`
* 关闭 `--disable-avdevice --disable-postproc --disable-doc --disable-iconv --disable-vulkan`
  （**保留 programs**：`ffmpeg` / `ffprobe` CLI 用于验证产物可用性，它们是独立可执行文件，不进入 SDK 链接）
* `--enable-small`；Windows 用 shared（dll + import lib），Android/iOS 用 static（`--disable-symver`）
* openssl 按 RTMPS 场景裁剪：`no-apps no-tests no-docs no-ssl3 no-weak-ssl-ciphers no-comp no-dtls no-psk no-srtp`
* 非 MSVC 编译加 `-ffunction-sections -fdata-sections`，最终链接 .so 时配 `-Wl,--gc-sections`
* OHOS 额外追加 `--enable-ohosavcodecdecoder`（见 `config.py::OHOS_FFMPEG_EXTRA_ARGS`）

参考体积（Windows x64）：`avcodec-60.dll 2.49MB`、`avformat-60.dll 4.82MB`、`avutil-58.dll 4.41MB`、
`swscale 0.59MB`、`avfilter 0.29MB`、`swresample 0.21MB`。

## 6. 环境要求与排错

| 平台 | 编译主机 | 工具链 |
| - | - | - |
| windows | Windows | VS 2022 Build Tools（MSVC）+ MSYS2（make/install/perl/nasm）+ cmake/ninja |
| android | macOS / Linux | Android NDK r21+（可 `bootstrap --install` 自动下载 r27c）+ cmake + make |
| ios | macOS | Xcode 命令行工具 + cmake + nasm（模拟器架构需要） |
| ohos | Linux / macOS（Windows 需 MSYS2/Git Bash） | DevEco Studio 或 commandline-tools 的 OHOS SDK（`$OHOS_SDK`）+ cmake + make + perl |

OHOS SDK 无法自动下载（需 DevEco Studio / commandline-tools），`bootstrap --platform ohos` 只做检测并给出指引。

常见问题：

* **缺 MSVC / Perl** → `python build.py bootstrap --platform windows --install`；
  需要管理员授权的步骤会只下载到 `tools/` 并打印命令，不会卡住脚本。
* **`make: cl: No such file or directory`** → 脚本已处理（MSYS 登录 shell 会重排 PATH，
  脚本内把 MSVC 路径以 `/c/...` 形式置顶）。若仍出现，检查 MSYS2 是否安装。
* **链接报 `__imp_xxx` 未解析** → CRT 混用；脚本已统一 `/MT`，不要单独改某个库的 CRT（见 6.1）。
* **`xxx not found using pkg-config`** → 脚本已处理（shell 内 export `PKG_CONFIG_PATH`、
  `.pc` 的 prefix 改写、openssl/srt 的库名与 .pc 归一化）。
* **ffmpeg 装到了奇怪目录** → 脚本已处理（Windows 路径转 `/d/...` 传给 bash）。
* 每个阶段的完整输出在 `build/logs/<lib>-<abi>-<stage>.log`。

### 6.1 Windows CRT 必须一致（`/MT`）

本工程在 Windows 上**全部按静态 CRT（`/MT`，Debug 为 `/MTd`）**构建，产物只适用于同样用 `/MT` 的消费方。
若消费方（如 MediaX 的某个模块 / VS 工程）是 `/MD`，链接时会出现 `__imp_xxx` 未解析或运行期堆损坏（跨 CRT 释放内存）。

各库如何固定 CRT：

| 库 | 做法 | 位置 |
| - | - | - |
| srt / fdk-aac / openh264 / libyuv / spdlog(可选) | `-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded` + `CMAKE_C(XX)_FLAGS_RELEASE=/MT ...` | `toolchains.py::WindowsToolchain.cmake_args()` |
| openssl | `Configure VC-WIN64A`（nmake 默认静态 CRT） | `recipes.py::build_openssl()` |
| ffmpeg | `--toolchain=msvc`，链接上面这些 `/MT` 静态库 | `recipes.py::build_ffmpeg()` |

消费方需要做的（二选一）：

```cmake
# CMake：在导入这些产物之前设置
cmake_policy(SET CMP0091 NEW)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
```

```
# VS 工程：属性 -> C/C++ -> 代码生成 -> 运行库 = 多线程 (/MT)，Debug 用 /MTd
```

若坚持用 `/MD`：必须**整条链路重编**——改 `toolchains.py::WindowsToolchain.cmake_args()` 里的
`MultiThreaded` → `MultiThreadedDLL`、`/MT` → `/MD`，并把 openssl 的 VC target 与 ffmpeg 一并重编；
只改其中某一个库必然出问题。

补充（spdlog）：产物是**编译型静态库**，链接方还需带 `SPDLOG_COMPILED_LIB`
（用 `find_package(spdlog)` 得到的 `spdlog::spdlog` target 会自动传播该宏；
手写 `-I.../include -lspdlog` 时必须自己加上，否则符号解析/ODR 出错）。

## 7. 与 MediaX 工程对接

> 说明：`MediaX/ESMediaX/3rd` 原有的构建脚本（`build_ffmpeg.py`、`build_fdkaac.py`、`build_openh264.py`、
> `build_spdlog.py`、`public.py`、`ohos_envset.sh`）已并入本工程并删除，该仓库现在只存放预编译产物。

产物目录结构与 `MediaX/ESMediaX/3rd` 一致，替换时：

```powershell
# 例：更新 Windows 开播使用的 ffmpeg 与 openh264（源目录即 -o 指定的输出目录）
Copy-Item D:\work\TGCapture3rd\ffmpeg\windows\x64\*   MediaX\ESMediaX\3rd\ffmpeg-capturer\windows\ -Recurse -Force
```

Android 的 libyuv 会额外产出 `libyuv_static.a`（兼容既有链接配置）；
Windows 的 ffmpeg 同时提供 `lib/*.lib`（import lib）与 `bin/*.dll`。

对接时注意两个硬约束：

* **CRT**：Windows 产物全是 `/MT`，MediaX 侧对应模块必须同为 `/MT`（详见 6.1）。
* **spdlog**：MediaX 的 `ESLogger` 依赖编译型 spdlog，版本锁 `v1.14.1`，且需 `SPDLOG_COMPILED_LIB`（详见 6.1）。

### 7.1 cuda-samples（NPP）：现状与将来建议

本工程**不构建** cuda-samples，也不产出任何 CUDA/NPP 相关内容；以下只是给 MediaX 侧的处置建议，
**当前保持不变**。

**现状**

| 项 | 情况 |
| - | - |
| 形态 | `MediaX/ESMediaX/3rd/cuda-samples` 是 3rd 仓库的 submodule（上游 `NVIDIA/cuda-samples`，锁定 `v11.8`，233.8 MB） |
| 谁在用 | 只有 Windows 的 `ESMediaXCudaNpp` 工程，且只用 `Common/helper_cuda.h`（header-only，**不链接它的任何库**） |
| 真正链接的库 | `cuda.lib`、`cudart.lib`、`nppc.lib`、`nppi*`×9、`npps.lib`，全部来自 CUDA Toolkit（`$(CUDA_PATH)\lib\x64`） |
| 开播是否依赖 | **否**。`capturer-winows-dll.vcxproj` 对它的 `ProjectReference` 已注释（注释：`requires CUDA 12.1 Toolkit`）；无 CUDA 时由 `core/video/CudaNppStub.cpp` 提供空实现，`canUse()` 恒为 false，走软转换 |

**将来建议（按推荐顺序）**

1. **保留 NPP 代码、移除 submodule（推荐）**：把实际用到的 `helper_cuda.h` 及其内联依赖的几个头文件
   拷贝进 `ESMediaXCudaNpp/`，然后从 3rd 仓库移除 submodule → 立省 **233.8 MB**，NPP 代码不受影响，
   将来要恢复也只是加回一条 submodule。
2. **彻底移除 NPP 加速**：额外删除 `ESMediaXCudaNpp/`、`core/video/NPPVideoMediaConvert.*`、
   `core/video/CudaNppStub.cpp` 及其调用点，并从 `ESCapture.sln`、`vs-sln/ESMediaX.sln` 摘除该项目。
3. **恢复引用（若日后要用 GPU 加速转缩放）**：必须先统一三件事——
   * CUDA 版本：注释要求 12.1、`capturer-winows-dll` 硬编码 `CUDA\v11.7`、链接库名是 CUDA 11 命名（升级需核对 `nppial/nppicc/...` 是否仍存在）；
   * 一律用 `$(CUDA_PATH)`，不要硬编码 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\vxx.x`；
   * `ESMediaXCudaNpp` 当前是 `/MD`，需改成 `/MT` 与其余产物一致（见 6.1）。
4. **无论选哪种**：`src/test/test.vcxproj` 里曾把包含目录写成 `../../../../3d/cuda-samples/common`
   （目录名与层级都错），已修正为 `../../3rd/cuda-samples/common`；该工程还链接了 FFmpeg 4.x 的
   `avcodec-58` / `avformat-58` 与一整套 CUDA 静态库，若要启用需一并更新到 ffmpeg 6.1 的 `avcodec-60` / `avformat-60`。

