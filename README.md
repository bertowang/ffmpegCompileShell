# FfmpegCompileShell

编译 **ffmpeg 及其依赖库** 的跨平台脚本（Python 3.8+，仅用标准库，零第三方依赖），
目标平台 Windows / Linux / Android / iOS / OHOS。
开播（capture）/ 播放（player）两套 profile；Windows 可选编入桌面硬编 `h264_nvenc` / `h264_amf`
（SDK 按锁定文件自动从上游拉取，见 [1.4](#14-windows-硬编nvenc--amf)）。

## 目录

- [0. 平台支持一览](#0-平台支持一览)
- [1. 依赖库清单](#1-依赖库清单)
  - [1.1 依赖锁定文件 `config/capture_deps.json`](#11-依赖锁定文件-configcapturedepsjson)
  - [1.2 profile：开播(capture) 与 播放(player)](#12-profile开播capture-与-播放player)
  - [1.3 Windows 运行时库 `--crt mt|md`](#13-windows-运行时库---crt-mtmd)
  - [1.4 Windows 硬编：NVENC / AMF](#14-windows-硬编nvenc--amf)
- [2. 产物输出目录（必填）](#2-产物输出目录必填)
- [3. 使用攻略](#3-使用攻略)
  - [3.1 Windows](#31-首次使用windows)
  - [3.2 Linux（本机 gcc）](#32-linux本机-gcc)
  - [3.3 iOS / Android](#33-macos-上编译-ios--android)
  - [3.4 OHOS](#34-ohosopenharmony)
  - [3.5 分步 / 增量](#35-分步--增量)
  - [3.6 常用参数](#36-常用参数)
- [4. 目录结构](#4-目录结构脚本入库源码产物不入库)
- [5. ffmpeg 裁剪策略](#5-ffmpeg-裁剪策略)
- [6. 环境要求与排错](#6-环境要求与排错)
  - [6.1 Windows CRT /MT](#61-windows-crt-必须一致mt)
- [7. 与 MediaX 工程对接](#7-与-mediax-工程对接)
  - [7.0 CMake 工程：`find_package(TGCapture3rd)`](#70-cmake-工程find_packagetgcapture3rd推荐)
  - [7.1 拷贝到 `MediaX/ESMediaX/3rd`](#71-拷贝到-mediaxesmediax3rd)
  - [7.2 cuda-samples（NPP）：现状与建议](#72-cuda-samplesnpp现状与将来建议)

> ⚠️ **产物输出目录必须显式指定**（`-o/--output` 或环境变量 `FFMPEG_3RD_OUTPUT`），
> 没有隐式默认值；漏给会直接报错退出，避免产物落到预期外的位置。详见第 2 节。

## 0. 平台支持一览

| 平台 | 编译宿主 | ABI：默认 / `--abi all` | ffmpeg 形态 | 平台编码器（白名单追加） | 版本文件关键信息 | 一键脚本 |
| - | - | - | - | - | - | - |
| windows | Windows（MSVC + MSYS2） | `x64` / `x64` `x86` `arm64` | **动态**：`bin/*.dll` + `lib/*.lib`（import lib） | 默认无；开关后 `h264_nvenc` / `h264_amf` | `crtmt` / `crtmd` | `scripts/build_windows.ps1` |
| linux | **Linux 本机**（gcc/clang，不支持交叉编译） | 宿主架构 / 展开为 `x86_64` `arm64` `armhf`，但只有宿主架构能编出 | 静态 | 无 | 无 | 无 |
| android | macOS / Linux（NDK） | `arm64-v8a` / `arm64-v8a` + `armeabi-v7a`（`x86_64` 需显式指定） | 静态 | `h264_mediacodec` | `api21` | `scripts/build_android.sh` |
| ios | macOS（Xcode） | `arm64` + `x86_64` lipo 成 fat 库 / 同默认 | 静态 | `h264_videotoolbox` | `min12.0` | `scripts/build_ios.sh` |
| ohos | Linux / macOS（OHOS SDK） | `arm64-v8a` / `arm64-v8a` `armeabi-v7a` `x86_64` | 静态（ffmpeg 用 openharmony-tpc fork） | 无 | 无 | 无 |

> **注意：**
> * **Windows 的三个 ABI 各用一套参数**：vcvarsall 目标（宿主 x64 上为 `x64` / `x64_x86` / `x64_arm64`）、
>   CMake VS 生成器 `-A x64|Win32|ARM64`、openssl `VC-WIN64A|VC-WIN32|VC-WIN64-ARM`、
>   ffmpeg `--arch=x86_64|x86|aarch64`、openh264 `ARCH=x86_64|x86|arm64`。
>   自检会按 cl.exe 所在目录（`Host<宿主>\<目标>`）核对 MSVC 是否真的面向该架构，缺组件时直接报错并给出 VS 组件名
>   （x86：`VC.Tools.x86.x64`，arm64：`VC.Tools.ARM64`），不会再编出“目录叫 x86、内容是 x64”的产物。
>   arm64 在 x64 宿主上是交叉编译：ffmpeg 加 `--enable-cross-compile`，构建后的组件自检（要运行目标程序）跳过；
>   ARM64 汇编需要 `gas-preprocessor.pl`（放进 PATH），没有时 ffmpeg `--disable-asm`、openh264 `USE_ASM=No`、openssl `no-asm`。
>   实测（VS 2022 / MSVC 14.44，x64 宿主）：x64 全套可用；x86 的 zlib / openh264 / libyuv 已实编，
>   经 `find_package` 链进 `-A Win32` 程序并运行通过，openssl / ffmpeg 的 x86 尚未实编；arm64 需先装 ARM64 组件，尚未实机验证。
> * **Linux 只做本机编译**：只用宿主 gcc，ffmpeg 也不加 `--enable-cross-compile`。要 arm64 产物，就到 arm64 机器上编。
> * `--abi` 多个值用**空格或逗号**分隔（`--abi x64 x86` / `--abi x64,x86`），支持 `all`（`*`）与平台别名，重复的自动去重。
>   **不认识的值一律报错**并列出可选值（以前 Windows 会悄悄回落为 x64）。
> * Windows 的 flat / lib 布局没有 abi 层：**同一个 `-o` 目录里先后单独编 x64、x86 会互相覆盖**。
>   要多个 ABI 就一次编（`--abi x64,x86`，自动改用 `<库>/windows/<abi>` 布局），或给每个 ABI 不同的 `-o`。
> * 平台名支持别名：`win`/`win64` → windows，`mac`/`iphoneos`/`darwin` → ios，
>   `openharmony`/`harmonyos`/`harmony`/`oh` → ohos，`linux64`/`gnu-linux` → linux。
> * 产物根目录自带 **CMake 包配置**（`find_package(TGCapture3rd)`），消费方怎么链接见[第 7 节](#7-与-mediax-工程对接)。

## 1. 依赖库清单

只保留开播（采集 → 编码 → 封装 → 推流）链路真正必须的库：

| 库 | 版本 | 作用 | 为什么必须 |
| - | - | - | - |
| zlib | 1.3.2 | 压缩 / 解压缩 | ffmpeg `--enable-zlib`，png 解码器（`inflate_wrapper`）的前提 |
| openssl | 3.6.5 | TLS / RTMPS | ffmpeg `--enable-openssl`，推流链路加密 |
| srt | 1.5.7 | SRT 推流 | ffmpeg `--enable-libsrt` |
| fdk-aac | 2.0.3 | AAC 音频软编码 | ffmpeg `--enable-libfdk-aac` |
| openh264 | 2.6.0 | H.264 视频软编码 | ffmpeg `--enable-libopenh264` |
| libyuv | 1974 | 格式转换 / 缩放 | 开播工程直接链接（不进 ffmpeg） |
| ffmpeg | 6.1.6 | 封装 + 协议 | muxer(flv/mpegts)、protocol(rtmp/rtmps/srt)；OHOS 改用 openharmony-tpc fork `ohos-n6.1.2_dev`（见 3.4） |

> 版本以 `config/capture_deps.json` 为准，上表与其同步；产物目录里的 `version-*.txt` 可复核实际版本。
> 上表是 `--libs all`（默认）构建的 7 个库；下面两个是可选库。

可选（默认不构建）：

| 库 | 版本 | 说明 |
| - | - | - |
| json | 3.12.0 | nlohmann/json，header-only；MediaX 侧通常已由 `ESMediaX/3rd/json` submodule 提供，需要时 `--libs json` |
| spdlog | v1.14.1 | C++ 日志库（编译型静态库）。只有 **MediaX** 需要：其 `ESLogger` 基于 spdlog 实现（android_sink / msvc_sink / OHOS hilog callback_sink + Windows Release 异步日志）；TGCapture 自带 `core/Logger.h`，不链。需要时 `--libs spdlog` |

文字场景（**默认不构建**，见 [1.5](#15-两种依赖场景现有依赖--带字符集字形库)）：

| 库 | 版本 | 说明 |
| - | - | - |
| freetype | 2.15.0 | 字形渲染（TrueType / OpenType）。**与 ffmpeg 无关**（ffmpeg 不开 `--enable-libfreetype` / drawtext），由 TGCapture 内核 `-DTG_WITH_TEXT=ON` 直接链接，用于时间水印的中文渲染。`--with-text` 追加构建，或 `--libs freetype` 单独补编 |

不使用 x264 / x265 / v265；cuda-samples(NPP) 经复核非开播编译期依赖，不纳入。

**AMF 与 nv-codec-headers** 是 Windows 硬编（`h264_amf` / `h264_nvenc`）的**构建期输入**（只要头文件，
不编译、运行期靠显卡驱动），默认不参与构建，需要时按 [1.4](#14-windows-硬编nvenc--amf) 打开开关。

### 1.5 两种依赖场景（现有依赖 / 带字符集·字形库）

部分场景只推流、画面上不需要文字；需要文字的场景才要字形库。**用 `--with-text` 区分，两个场景分别发起编译与集成**：

| 场景 | 命令 | 产物 | 消费方 |
| - | - | - | - |
| 2.1 现有依赖（默认） | `python build.py build -p <平台> -o <输出根>` | zlib / openssl / srt / fdk-aac / openh264 / libyuv / ffmpeg | 正常构建，不链任何字符集库 |
| 2.2 支持字符与时间显示 | `python build.py build -p <平台> --with-text -o <输出根>` | 上面 7 个 **+ freetype** | `-DTG_WITH_TEXT=ON` |

要点：

* **ffmpeg 不需要重编**：freetype 不在 `ffmpeg_deps` 组，不产生任何 `--enable-*` 开关，两个场景的
  ffmpeg 产物**完全一致**（同样的 configure、同样的白名单）。差别只是产物目录里多一个 `freetype/`。
  因此 `--with-text` 也可以只在已有产物上补编：`python build.py build -p <平台> --libs freetype -o <已存在的输出根>`。
* **默认构建不会引入 freetype**：`--libs all`（默认）只包含必须库；不想要文字的场景不会拉进无关依赖。
* 想让两个场景的产物**物理分开**（交付时只带需要的），给不同的 `-o`（例如 `TGCapture3rd` 与 `TGCapture3rd-text`）。
* 升级本工程后，旧产物目录要补一次 `python build.py export -o <产物根目录>` 才会认识 `freetype` 组件
  （包配置是每次 `build` / `export` 时刷到产物根目录的）。
* CMake 包配置（`find_package(TGCapture3rd)`）按**组件**暴露：`COMPONENTS ffmpeg libyuv` 不含 freetype，
  加 `freetype` 才会去找 freetype 产物（找不到即 `TGCapture3rd_freetype_FOUND=0` 并给出补编提示）。
  头文件目录为 `<输出根>/freetype/<平台>[/<abi>]/include/freetype2`（`#include <ft2build.h>`）。
* freetype 按最小化构建：zlib / bzip2 / png / harfbuzz / brotli / HVF 全关（macOS 15+ SDK 自带 HVF，
  不关会引用系统私有库 `libhvf` 的符号），**零传递依赖**，只链 `libfreetype.a` / `freetype.lib` 本身；
  授权为 FTL（BSD 风格，需在产品文档致谢）/ GPLv2 双授权。

### 1.1 依赖锁定文件 `config/capture_deps.json`

所有依赖的 **地址 / 版本 / commit** 统一锁在独立的 `config/` 目录里，按**用途**分文件命名：

| 文件 | 用途 | 状态 |
| - | - | - |
| `config/capture_deps.json` | **开播**（capture） | 当前的正式配置，默认加载 |
| `config/player_deps.json` | **播放**（player） | 示例骨架：`ffmpeg 6.1.6` + `zlib`（png 解码依赖），可选 `json` / `spdlog`（不含 openssl/srt/fdk-aac/openh264/libyuv） |

两份文件互不干扰：`config.py` 只加载本次指定的那一份，**锁定文件里没列的库不会进入构建集合**
（所以 player 下 `python build.py list` 只会看到必须库 zlib / ffmpeg 与可选库 json / spdlog）。

**只要确定这个文件，任何时候都能编出完全相同的目标库**，`config.py` 只保留用途描述。
唯一例外是 OHOS 的 ffmpeg：fork 只锁了分支（`commit` 为空），按分支最新提交拉取，不保证可复现（见 3.4）。

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

分四组（与 ffmpeg 的耦合关系）：

| 组 | 含义 | 库 |
| - | - | - |
| `ffmpeg` | FFmpeg 自身源码，按**用途(profile)**再分档 | `ffmpeg`（capture 文件里 profile=**capture**；player 文件里 profile=**player**） |
| `ffmpeg_deps` | FFmpeg 的**构建期依赖**，需对应 `--enable-*`，产物静态并入 avcodec/avformat | `zlib`、`openssl`、`srt`、`fdk-aac`、`openh264`（player 只有 `zlib`） |
| `standalone` | 与 FFmpeg **无关**的独立库，应用直接链接 | `libyuv`、`json`、`spdlog` |
| `ffmpeg_hw_sdks` | 桌面**硬编 SDK**：FFmpeg 的构建期输入（头文件，不编译、不随产物分发） | `amf`、`nv-codec-headers`（仅 Windows + 显式开关，见 1.4） |

规则：

* **内网仓库**（`EST/media/e-ffmpeg`）：`ffmpeg`（ffmpeg-6.1.6）、`fdk-aac`（fdk_aac_v2.0.3）。
* **硬编 SDK 用上游官方仓库并锁 tag**：`amf` → `GPUOpen-LibrariesAndSDKs/AMF` `v1.4.34`、
  `nv-codec-headers` → `FFmpeg/nv-codec-headers` `n12.0.16.1`（两者都同时锁 commit，见 1.4）。
* 其余库用**上游官方仓库**，取锁定日（`updated` 字段）的最新正式发布版；
  个别库为兼容 MediaX 会锁在旧版（如 `spdlog` 锁 v1.14.1，条目里有 `note` 说明原因）。
* ffmpeg 的 `capture`（开播）标记会写进版本文件名，与 `player` 产物区分（完整格式见 2.2）：

  ```
  version-x64-ffmpeg-6.1.6-g3c1fc5d-2026-10-21-10-14.windows.x64.capture.msvc.crtmt.txt
  version-x64-spdlog-v1.14.1-g27cb4c7-2026-10-20-14-35.windows.x64.msvc.crtmt.txt
  ```
* git 类依赖一律锁到 **commit**（不是 tag），归档类锁到 **release tag**（可选填 `sha256`）。
  例外：OHOS 的 ffmpeg fork 只锁分支（见 3.4）。
* 源码目录会写入 `.deps_pin`；若锁定文件改了版本，下次构建会**自动重新拉取**，
  不会误用旧源码（这是可复现的关键）。

升级某个库，只改 `config/capture_deps.json` 对应条目即可，无需动 `config.py`。

### 1.2 profile：开播(capture) 与 播放(player)

`profile = 用途`。它由 `--profile` 决定，并同时驱动四件事，两项用途互不干扰：

| 维度 | capture（开播） | player（播放） |
| - | - | - |
| 锁定文件 | `config/capture_deps.json` | `config/player_deps.json` |
| 参与构建的库（`--libs all`） | 7 个：zlib / openssl / srt / fdk-aac / openh264 / libyuv / ffmpeg；可选 json / spdlog | 2 个：zlib / ffmpeg；可选 json / spdlog |
| ffmpeg 外部库开关 | `--enable-zlib --enable-openssl --enable-libsrt --enable-libfdk-aac --enable-libopenh264`（**来自锁定文件 `ffmpeg_deps` 组的 `ffmpeg_flag`**，非硬编码）；开硬编时再追加 `--enable-ffnvcodec` / `--enable-d3d11va`（来自 `ffmpeg_hw_sdks` 组，见 1.4） | `--enable-zlib`（png 解码依赖 zlib） |
| 裁剪白名单（`config.PROFILE_WHITELIST`） | 编码器 `libopenh264,libfdk_aac,aac`（+ 平台编码器：Android `h264_mediacodec`、iOS `h264_videotoolbox`、Windows 开开关后 `h264_nvenc`/`h264_amf`；Linux / OHOS 不追加）、封装 `flv,mpegts`、解封装含 `image2`（水印图）、协议 `file,tcp,tls,http,https,rtmp,rtmps,libsrt`（完整列表见第 5 节） | **不生成** `--enable-encoder` / `--enable-muxer`；解码器 `h264,hevc,aac,mp3,png,mjpeg`、解封装 `mov,flv,matroska,mpegts,hls`（mp4/m4a 由 `mov`、mkv/webm 由 `matroska` 处理）、协议 `file,http,tcp,hls` |

白名单细节（都是踩过坑才改的）：

* 白名单写的是 **configure 的内部组件名**，不是文件扩展名：`mkv` → `matroska`、`mp4` → `mov`。
  configure 对未知名称只打一行 WARNING 然后忽略，产物就悄悄缺组件。
  现在 `build` 在 configure 之前会对照该 ffmpeg 源码的登记表（`libavcodec/allcodecs.c`、`libavformat/allformats.c`、
  `protocols.c`、`parsers.c`、`bitstream_filters.c`、`libavfilter/allfilters.c`）逐个校验，名字不存在就直接报错；
  登记表解析不出任何名字（换了大版本 / fork 格式不同）时只告警跳过，不误报。
* 协议名要用 **`libsrt`**（协议内部名是 `libsrt`，URL scheme 才是 `srt`）；
  写成 `srt` 会被 configure **静默忽略**，结果 `ffmpeg -protocols` 里没有 `srt`，SRT 推流不可用。
* **`--enable-zlib` 是 png 解码器的前提**：`png_decoder_select="inflate_wrapper"` → zlib，
  缺 zlib 时 `CONFIG_PNG_DECODER=0`，水印/logo 这类 PNG 图片解码不了（配合 `http/https` 协议 +
  `image2` 解封装才能加载远程水印图）。

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
* ffmpeg 的版本标记文件名带 profile：`version-x64-ffmpeg-6.1.6-g…-….windows.x64.player.msvc.crtmt.txt`。

补充：player 目前没有 openssl，因此协议只开到 `http`/`hls`；
若播放器需要 https/tls，把 `openssl` 加进 `config/player_deps.json` 的 `ffmpeg_deps` 组即可，
`--enable-openssl` 会由 `ffmpeg_flag` 自动带上。

### 1.3 Windows 运行时库 `--crt mt|md`

| 取值 | 含义 | 适用 |
| - | - | - |
| `mt`（**默认**） | `/MT` 静态链接 CRT | 与现有产物一致：openssl/ffmpeg/CMake 库全部 `/MT`（ffmpeg 的 DLL 里不含 `MSVCR140.dll`） |
| `md` | `/MD` 动态链接 CRT | 消费方工程是 `/MD` / `/MDd` 时（如 ESMediaX 全工程 `MultiThreadedDLL`） |

```bash
python build.py build --platform windows --crt md -o D:/work/3rd-md --libs spdlog,libyuv,json
```

* 作用域：**只对 CMake 构建的库生效**（`zlib` / `srt` / `fdk-aac` / `libyuv` / `spdlog`）。
  `openh264` 用自带 Makefile（`OS=msvc`）构建，`openssl` 用 nmake，`ffmpeg` 用 configure，三者都不受 `--crt` 控制，
  始终是 `/MT`（ffmpeg 是 DLL 且为 C 接口，`/MT` 的 DLL 被 `/MD` 的 exe 加载没问题）；`json` 是 header-only，与 CRT 无关。
* 因此 `--crt md` **不要用来编 ffmpeg**：会把 `/MD` 的静态库链进 `/MT` 的 ffmpeg。
  典型用法是只给「应用直接链接的库」单独出一份 `/MD`。
* 版本标记文件里会多一行 `crt : /MD`（或 `/MT`），可据此确认这批库的 CRT。
* 消费方报 `检测到"RuntimeLibrary"的不匹配项` 时：
  `dumpbin /directives xxx.lib` 看 `RuntimeLibrary=MD_DynamicRelease` 还是 `MT_StaticRelease`，
  再与工程的 `MultiThreaded`/`MultiThreadedDLL`（Debug 为 `…Debug`/`…DebugDLL`）比对。

### 1.4 Windows 硬编：NVENC / AMF

Windows 开播除了软编 `libopenh264`，还可以编进**桌面独显硬编**（两者并存，上层按可用性选择，
拿不到硬编时仍回退软编）：

| 编码器 | 显卡 | 构建期需要的 SDK | 运行期依赖 |
| - | - | - | - |
| `h264_nvenc` | NVIDIA | `nv-codec-headers`（`ffnvcodec/nvEncodeAPI.h` + `ffnvcodec.pc`） | 驱动自带的 `nvEncodeAPI64.dll` |
| `h264_amf` | AMD | `amf`（`AMF/core/*.h`、`AMF/components/*.h`） | 驱动/AMD 运行时自带的 `amfrt.dll` |

**开关（默认都关）**：

```powershell
# 只编 ffmpeg，两个硬编都要
python build.py build --platform windows -o D:/work/TGCapture3rd --libs ffmpeg -j 16 --enable-nvenc --enable-amf

# 只要其中一个
python build.py build --platform windows -o D:/work/TGCapture3rd --libs ffmpeg --enable-nvenc
```

编完自检（两个都应出现）：

```powershell
D:\work\TGCapture3rd\ffmpeg\windows\bin\ffmpeg.exe -hide_banner -encoders | findstr "h264_nvenc h264_amf"
```

要点：

* **SDK 不用自备**，也不用 `--libs` 指定：锁定文件 `ffmpeg_hw_sdks` 组里写死了地址与版本，
  开了开关就自动拉取；它们是"构建期输入"不是"要编译的库"，所以 `build.py list` 里看不到。
* **来源与版本**：直接用上游 GitHub 并锁 tag —— `amf` 取 `v1.4.34`（FFmpeg 6.1 要求 `AMF >= 1.4.29`）、
  `nv-codec-headers` 取 `n12.0.16.1`（对应 Video Codec SDK 12.0.16，与 ffmpeg 6.1.6 匹配）；
  两个都同时锁 commit，升级只改锁定文件。
* **AMF 仓库很大，已做提速**：上游仓库带 `Thirdparty/`（DeckLink SDK、预编译 ffmpeg-6.0 等约 490MB），
  而构建只用 `amf/public/include`。因此走 `--depth 1` + `--filter=blob:none` + sparse-checkout，
  实测落地 **1.45MB**，`Thirdparty/` 根本不下载（锁定文件里 `"sparse": ["amf/public/include"]`）。
* **头文件落位**（各 SDK 官方 `make install` 布局，各自独立目录，**不塞进 `ffmpeg/`**，
  避免整目录拷贝 ffmpeg 时被一起覆盖/删掉）：

  ```
  <输出>/amf/windows/include/AMF/...                       + version-…-amf-1.4.34-g6d7bec0-….txt
  <输出>/nv-codec-headers/windows/include/ffnvcodec/*.h    + version-…-12.0.16.1-g451da99-….txt
  <输出>/nv-codec-headers/windows/lib/pkgconfig/ffnvcodec.pc
  ```

  上游 AMF 仓库里没有 `AMF/` 这一层（是 `amf/public/include/{core,components}`），
  FFmpeg 写的是 `#include <AMF/core/Version.h>`，安装时会规整出 `AMF/` 前缀。
* **configure 上实际追加的开关**（来自锁定文件的 `ffmpeg_flag` + `extra_flags`）：

  | 开关 | 追加 | 为什么 |
  | - | - | - |
  | `--enable-nvenc` | `--enable-ffnvcodec` | `--disable-everything` 会把**外部库 ffnvcodec** 一起关掉，不开则 `nvenc requested, but not all dependencies are satisfied` |
  | `--enable-amf` | `--enable-d3d11va` | AMF 内部走 D3D11（`CONFIG_D3D11VA`），采集链路本就是 D3D11/DXGI；`--disable-everything` 同样会关掉这个 hwaccel |

* **为什么移动端不加**：iOS/Android 分别用 `h264_videotoolbox` / `h264_mediacodec`
  （`config.py::whitelist_encoders` 的 `platform_encoders`），它们不需要任何外部 SDK；
  且移动端对体积极敏感，桌面硬编 SDK 与代码一律不带过去 —— 即使命令行给了
  `--enable-nvenc --enable-amf`，非 Windows 平台也拿不到这两个编码器
  （`HARDWARE_ENCODERS` 只登记了 `windows`）。
* **运行期不需要随包任何 SDK 文件**：头文件只是编译期声明，实际编解码由显卡驱动提供；
  所以不要把 `amf/` / `nv-codec-headers/` 拷进应用的 3rd 目录。

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

### 2.1 产物目录布局（对齐 `MediaX/ESMediaX/3rd`）

Windows 下按 **3rd-bak 的历史结构**输出，产物可以整目录拷进 `ESMediaX/3rd` 直接替换：

| 布局 | 结构 | 对应库 | 3rd-bak 里的样例 |
| - | - | - | - |
| `flat` | `<库>/windows/{include,lib,bin}`（**无 abi 层**） | `ffmpeg` `libyuv` `zlib` `openssl` `srt` `fdk-aac` `openh264` | `ffmpeg-capturer/windows/{bin,include,lib}`、`libyuv/windows/{bin,include,lib}` |
| `lib` | `<库>/windows/include` + `<库>/windows/lib/<abi>` | `spdlog` | `spdlog/windows/lib/x64`、`spdlog/windows/lib/x86` |
| `abi` | `<库>/windows/<abi>/{include,lib,bin}`（本平台默认） | 未登记的库；以及**同时编多个 ABI** 时自动回落 | — |

其它平台同样以 `PLATFORM_LAYOUT` 对齐 `3rd-bak`：

| 平台 | 布局 | 说明 |
| - | - | - |
| linux | `abi`：`<库>/linux/<abi>/{include,lib,bin}`（spdlog 也一样） | 无 3rd-bak 历史包袱，统一带 abi 层 |
| android | `abi`（默认）：`<库>/android/<abi>/{include,lib,bin}`；`spdlog` → `flat` | 对齐 `ffmpeg-capturer/android/arm64-v8a/{bin,include,lib,share}`、`spdlog/android/{include,lib}` |
| ohos | 同上（`libyuv/ohos/<abi>`、`spdlog/ohos/{include,lib}`） | 对齐 3rd-bak |
| ios | `flat` + lipo 合并：**`<库>/ios/{include,lib}`（fat，无 abi 层）** | 对齐 `ffmpeg-capturer/ios/{include,lib}`、`libyuv/ios/{include,lib}` |

* 总表是 `config.PLATFORM_LAYOUT`（Windows 那张是其中的 `WINDOWS_LAYOUT`），加新库时在对应平台登记一行即可；
  未登记的库用平台默认布局（iOS 为 `flat`，其余为 `abi`）。
* 硬编 SDK（`amf` / `nv-codec-headers`）同样是**无 abi 层的 flat**：`<库>/windows/{include,lib}`，
  且只在开了 `--enable-amf` / `--enable-nvenc` 时才产出，属于构建期输入（见 1.4）。
* flat / lib 布局没有 abi 层，一次同时编多个 ABI 会互相覆盖 → 检测到多 ABI 时自动改用 abi 目录并打印提示
  （iOS 除外：多个 ABI 本来就要 lipo 成一个 fat 库）。

**portable（与平台无关的库）**：header-only 的 `json` 在锁定文件里声明 `"layout": "portable"`，
产物直接放 `<输出根>/json/…`，**不带 `<平台>`/`<abi>` 层**，所有平台共用一份
（与 `ESMediaX/3rd` 里 json submodule 的布局一致）。
输出哪些子目录由同一个条目的 `"output_subdirs"` 决定，现在只给 `single_include`——
即 `<输出根>/json/single_include/nlohmann/json.hpp`（**合并后的单头文件**，与 `3rd-bak` 一致；
源码里的 `include/` 是 46 个模块化头文件，需要时把 `output_subdirs` 改成 `["include","single_include"]` 即可）。
版本标记相应写成
`version-json-3.12.0-2026-10-20-11-52.any.header-only.txt`（平台 `any`、无 abi、编译器 `header-only`）。
判定函数 `config.is_portable(lib)`，加新的 header-only 库只要在锁定文件里加一行 `"layout": "portable"`。

### 2.2 版本标记文件名

```
version-<abi>-<库>-<版本>-g<commit>-<构建时间>.<平台>[.<abi>][.<用途>].<编译器>[.<关键信息>].txt
例：version-x64-ffmpeg-6.1.6-g3c1fc5d-2026-10-21-10-14.windows.x64.capture.msvc.crtmt.txt
    version-x64-spdlog-v1.14.1-g27cb4c7-2026-10-20-09-05.windows.x64.msvc.crtmd.txt
    version-x64-openssl-3.6.5-2026-10-20-14-27.windows.x64.msvc.crtmt.txt        （归档类库无 -g<commit>）
    version-x86_64-ffmpeg-6.1.6-gf1e3a2b-….linux.x86_64.capture.gcc.txt         （Linux 无关键信息）
    version-arm64-v8a-ffmpeg-6.1.6-g3c1fc5d-…android.arm64-v8a.capture.ndk-clang.api21.txt
    version-arm64-x86_64-ffmpeg-6.1.6-g3c1fc5d-….ios.arm64-x86_64.capture.xcode-clang.min12.0.txt（iOS fat：abi 记为 arm64-x86_64）
    version-json-3.12.0-2026-10-20-11-56.any.header-only.txt                     （portable 库）
```

**abi 放在最前面**，一眼分清 x64 / arm64-v8a / arm64-x86_64(fat)；末尾的**关键信息**按平台记最要紧的构建约束
（与 `3rd-bak` 里 `ffmpeg-6.0-…-min13.0.txt` 的做法一致）：

| 平台 | 编译器标识 | 关键信息 | 来源 |
| - | - | - | - |
| windows | `msvc` | `crtmt` / `crtmd` | `--crt` |
| linux | `gcc` / `clang`（按实际 cc 判断） | 省略 | — |
| android | `ndk-clang` | `api21` | `--api` |
| ios | `xcode-clang` | `min12.0`（默认） | `--min-version` |
| ohos | `ohos-clang` | 省略 | — |
| portable（json） | `header-only` | 省略（平台记为 `any`，无 abi） | — |

硬编 SDK（amf / nv-codec-headers）的编译器标识也是 `header-only`，但仍带平台、abi 与 `crt…`。
文件里另有 `profile` / `group` / `crt` / `variant` / `layout` 等行。

产物布局（与 `MediaX/ESMediaX/3rd/<库>/<平台>/<abi>` 一致，可直接替换）：

```
TGCapture3rd/
├── zlib/windows/{include,lib}
├── openssl/windows/{include,lib}
├── srt/windows/{include,lib}
├── fdk-aac/windows/{include,lib}
├── openh264/windows/{include,lib}
├── libyuv/windows/{include,lib}
├── ffmpeg/windows/{include,lib,bin,share}
├── spdlog/windows/{include,lib/x64}           # 可选库，lib 布局
├── json/single_include/nlohmann/json.hpp      # 可选库，portable，无平台层
├── amf/windows/include/AMF/...                # 仅 --enable-amf：构建期输入
├── nv-codec-headers/windows/{include,lib}     # 仅 --enable-nvenc：构建期输入
├── */linux/<abi>/...
├── */android/<abi>/...                        # spdlog 例外：spdlog/android/{include,lib}
├── */ohos/<abi>/...                           # 同 android
└── */ios/...                                  # iOS 为 fat 库，不带 abi 层
```

每个库目录下都会生成一个上述格式的**版本标记文件**，便于追溯（文件名格式见 [2.2](#22-版本标记文件名)）。

* `-g<commit>`：源码目录的短 commit（7 位）；源码是压缩包（zlib / openssl / srt / openh264 / json）时省略。
  fdk-aac、libyuv、spdlog、ffmpeg、硬编 SDK 都是 git 拉取，带 `-g`
* 编译器标识：`msvc` / `gcc` / `clang` / `ndk-clang` / `xcode-clang` / `ohos-clang` / `header-only`
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

# ③' Windows 开播要硬编时（可选）：加 --enable-nvenc / --enable-amf，
#     SDK 会按锁定文件自动从上游拉取，不用自备（详见 1.4）
python build.py build --platform windows -o D:/work/TGCapture3rd --enable-nvenc --enable-amf

# ④ 查看产物
python build.py list
dir D:\work\TGCapture3rd
```

> 默认（不加开关）只编软编 `libopenh264`，产物最小；硬编 SDK 需要联网拉取，
> 且只在 Windows 生效 —— iOS/Android 用 `h264_videotoolbox` / `h264_mediacodec`，无需 SDK。

一键脚本：`powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 [-Libs all] [-Abi x64] [-Jobs 0]`
（先 `bootstrap --install` 再 `build`；脚本**不传 `-o`**，需先设好环境变量 `FFMPEG_3RD_OUTPUT`，否则报错退出）

### 3.2 Linux（本机 gcc）

服务端开播 / 本机验证：**本机编译，不需要也不支持交叉工具链**（gcc + make + cmake +
pkg-config + perl；nasm 可选）。必须在 Linux 宿主上运行，其它宿主直接报错。

```bash
# ① 自检（缺什么就给出 dnf/apt 安装命令，--install 也只打印命令不代装；nasm 缺失只是降级，不阻断）
python3 build.py bootstrap --platform linux

# ② 编译开播全套 -> <输出目录>/<库>/linux/<宿主架构>
python3 build.py build --platform linux -o ~/TGCapture3rd-linux

# 只编 ffmpeg（依赖已就绪时）
python3 build.py build --platform linux -o ~/TGCapture3rd-linux --libs ffmpeg
```

要点：

* **ABI 默认取宿主架构**（`x86_64` / `arm64` / `armhf`），不必显式给 `--abi`。
  由于只做本机编译（宿主 gcc，ffmpeg 不加 `--enable-cross-compile`），`--abi all` 虽会展开成三个架构，
  **只有与宿主一致的那个能编出可用产物**；其它架构请到对应机器上编。
* 产物是**静态库**（`--enable-static --disable-shared`，与 Android/iOS 一致），ffmpeg 额外链接
  `-lm -lpthread -ldl -lstdc++`。`ffmpeg/linux/<abi>/bin/ffmpeg` 可直接执行，构建结束会**自动跑一次组件自检**
  （白名单里的编码器 / 协议 / 滤镜是否真编进去了，缺了只 WARN 不中断）——
  这是本机编译平台（Windows / Linux）独有的好处，交叉编译的平台跑不了产物。
* **x86 汇编需要 nasm**；没有时自动 `--disable-x86asm`（openh264 用 `USE_ASM=No`），
  不会编译到一半才失败。注意脚本把 `yasm` 也当作可用，但 ffmpeg 6.1 的 x86 汇编需要 nasm，请装 nasm。
* **内网仓库不可达时的兜底**：`ffmpeg` 与 `fdk-aac` 在锁定文件里带 `platform_overrides.linux`，
  改用上游 GitHub 的同版本（ffmpeg → `n6.1.6`；fdk-aac → `v2.0.3`，其 commit 与内网版完全相同）。
  这是**通用的按平台换源**能力（任何库都能写 `platform_overrides`），其它平台仍走内网。
* **RHEL 系发行版**（TencentOS / CentOS / Rocky）上 CMake 与 openssl 默认装到 `lib64`，
  而 ffmpeg 的 configure 固定按 `<prefix>/lib` 找依赖 —— 脚本会在安装后把 `lib64`
  收敛到 `lib`，产物布局与其它平台一致（见 `recipes.py::collapse_lib_dirs`）。

### 3.3 macOS 上编译 iOS / Android

```bash
python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios -o ~/TGCapture3rd                 # arm64 + x86_64 -> lipo 合并成 fat 库

python3 build.py bootstrap --platform android --install
python3 build.py build --platform android --abi arm64-v8a --api 21 -o ~/TGCapture3rd
python3 build.py build --platform android --abi all -o ~/TGCapture3rd   # arm64-v8a + armeabi-v7a
```

**iOS**

* ABI：默认（以及 `fat` / `all`）= `arm64` 真机 + `x86_64` 模拟器，lipo 合并后输出到 `<库>/ios/{include,lib}`；
  也可只编一个：`--abi arm64`（别名 `device`）或 `--abi x86_64`（别名 `simulator`）。
* 最低版本默认 `12.0`（`--min-version`），会写进版本文件名。
* ffmpeg：静态库，`--disable-asm`（iOS 汇编需要 gas-preprocessor）+ `--enable-videotoolbox`。
* `bootstrap --platform ios` 只有目标 ABI 含 `x86_64`（模拟器的 openh264 汇编要用）时才要求 nasm；
  只编真机（`--abi arm64`）时 nasm 是可选项。`--install` 会用 brew 装上 cmake / nasm / pkg-config。

**Android**

* ABI：`arm64-v8a`（默认）/ `armeabi-v7a` / `x86_64`；`--abi all` 只展开成 `arm64-v8a` + `armeabi-v7a`，
  要 `x86_64`（模拟器）需显式写上，例如 `--abi arm64-v8a armeabi-v7a x86_64`。
* API 级别默认 `21`（`--api`）；NDK 按 `--ndk` / `ANDROID_NDK_HOME` 等路径查找，代码不校验 NDK 版本，推荐 r27c。
* ffmpeg：静态库 + `--disable-symver`，并加 `--enable-jni --enable-mediacodec`（`h264_mediacodec` 硬编）。
* `bootstrap --platform android` 检查 NDK / cmake / make / **perl / pkg-config**（openssl 的 Configure 是 perl 脚本，
  ffmpeg 的 configure 靠 pkg-config 找 openssl / srt / fdk-aac / openh264）。
  `--install` 会自动下载 NDK r27c；macOS 上再用 brew 装缺的 perl / pkg-config / cmake / make，Linux 上只打印 apt / dnf 命令。

一键脚本（都会先跑 `bootstrap --install`）：

| 脚本 | 参数 | 输出目录 |
| - | - | - |
| `./scripts/build_ios.sh [fat\|arm64\|x86_64] [输出目录]` | ABI 默认 `fat` | 依次取第二个参数、`$FFMPEG_3RD_OUTPUT`、`../TGCapture3rd`（**唯一有隐式默认目录的入口**） |
| `./scripts/build_android.sh [abi] [api]` | 默认 `arm64-v8a` / `21` | **不传 `-o`**：必须先设 `FFMPEG_3RD_OUTPUT`，否则报错退出 |

### 3.4 OHOS（OpenHarmony）

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

* ABI：`arm64-v8a` / `armeabi-v7a` / `x86_64`，默认 `arm64-v8a`；`--abi all` 三个全编。
* cmake 优先用 SDK 自带的 `native/build-tools/cmake/bin/cmake`（有 SDK 的 ninja 时用 Ninja，否则 Unix Makefiles），并注入
  `-DOHOS_PLATFORM=OHOS -DOHOS_ARCH=<abi> -DCMAKE_TOOLCHAIN_FILE=<sdk>/native/build/cmake/ohos.toolchain.cmake`。
* openssl 走 `linux-aarch64` / `linux-armv4` / `linux-x86_64` target；openh264 走 `make OS=linux ARCH=<arch>`。
* ffmpeg 是静态库（`--disable-x86asm --disable-symver`），没有平台编码器，开播只用软编。
* ffmpeg 源码默认换成 openharmony-tpc 的 fork（锁定文件 `platform_overrides.ohos`：分支 `ohos-n6.1.2_dev`），
  并追加 `--enable-ohosavcodecdecoder`（来自 `config.py` 的 `OHOS_FFMPEG_EXTRA_ARGS`，只有该 fork 支持）。
  * 这个 fork **只锁了分支、没锁 commit**，每次按分支最新提交拉取，产物不保证可复现。
  * 要改用官方 ffmpeg，需要**同时**：删掉 `config/capture_deps.json`（以及 `player_deps.json`）里 ffmpeg 条目的
    `platform_overrides.ohos`，并把 `OHOS_FFMPEG_EXTRA_ARGS` 置空。只置空开关的话，源码仍然是 fork。
* 宿主用 Linux / macOS。代码里有 Windows 宿主的雏形（调用 MSYS2 / Git Bash），但未经验证，不建议使用。

### 3.5 分步 / 增量

```bash
set OUT=D:/work/TGCapture3rd
python build.py build --platform windows --libs openssl -o %OUT%   # 只编一个库
python build.py build --platform windows --libs spdlog  -o %OUT%   # 可选库：MediaX 的日志实现
python build.py build --platform windows --libs ffmpeg   -o %OUT%  # 依赖已就绪时单独重编 ffmpeg
python build.py build --platform windows --libs ffmpeg   -o %OUT% --enable-nvenc --enable-amf  # 带硬编重编
python build.py build --platform windows --clean         -o %OUT%  # 清理后全量重建
python build.py clean --platform windows --all           -o %OUT%  # 只清理（含产物）
python build.py build --platform windows -j 8            -o %OUT%  # 指定并发
```

> ffmpeg 构建前会按锁定文件 `ffmpeg_deps` 组校验依赖是否已产出（capture：`zlib / openssl / srt / fdk-aac / openh264`；
> player：`zlib`），未就绪会直接提示"请先编译依赖"，而不是报含糊的 `xxx not found`。

### 3.6 常用参数

`fetch` / `build` / `clean` 共用：

| 参数 | 说明 | 默认 |
| - | - | - |
| `-p/--platform` | `windows` / `linux` / `android` / `ios` / `ohos`（别名见第 0 节） | 必填 |
| `--abi` | 多个值用**空格或逗号**分隔，不认识的值直接报错。windows `x64`/`x86`/`arm64`；linux `x86_64`/`arm64`/`armhf`；android `arm64-v8a`/`armeabi-v7a`/`x86_64`；ios `arm64`/`x86_64`/`fat`（别名 `device`/`simulator`）；ohos `arm64-v8a`/`armeabi-v7a`/`x86_64`；`all` 的含义按平台不同（见第 0 节）。`bootstrap` 也接受 `--abi`，按目标 ABI 自检 | 平台默认（第 0 节） |
| `--libs` | 逗号分隔，`all` 为必须库全集（不含可选的 json / spdlog 与文字场景的 freetype） | `all` |
| `--with-text` | 文字场景：在 `all` 之外再编 freetype（时间水印的中文 / TrueType）。两个场景的 ffmpeg 产物相同，详见 [1.5](#15-两种依赖场景现有依赖--带字符集字形库) | 关 |
| `-o/--output` | 产物输出目录（`fetch` 不需要） | **必填**（或环境变量 `FFMPEG_3RD_OUTPUT`） |
| `-j/--jobs` | 并行任务数 | CPU-1 |
| `--toolchain` | Windows 工具链偏好 `auto`/`msvc`/`mingw`。`mingw` 尚未打通（openssl 需要 nmake，ffmpeg / openh264 写死了 msvc），实际只能用 MSVC | `auto`（优先 MSVC） |
| `--crt` | Windows 运行时库 `mt` / `md`，只对 CMake 构建的库生效（见 1.3） | `mt` |
| `--api` / `--min-version` | Android API / iOS 最低版本 | 21 / 12.0 |
| `--ndk` | Android NDK 路径；不给则依次查 `ANDROID_NDK_HOME` / `ANDROID_NDK_ROOT` / `ANDROID_HOME/ndk/*` 等 | 自动查找 |
| `--ohos-sdk` | OHOS SDK 根目录（含 `native/`），不给则取 `$OHOS_SDK` | `$OHOS_SDK` |
| `--force-fetch` | 强制重新拉取源码 | 关 |
| `--profile` / `--config-dir` / `--deps` | 选择锁定文件（见 1.1）；`list` / `bootstrap` 也支持 | `capture` |

仅 `build`：

| 参数 | 说明 | 默认 |
| - | - | - |
| `--clean` | 编译前清理中间目录与旧产物 | 关 |
| `--dry-run` | 只打印命令不执行（不落盘产物，也不写版本标记 / CMake 包配置） | 关 |
| `--no-check` | 跳过构建前的工具链自检（自检只检查不安装） | 关 |
| `--ffmpeg-ref` | 覆盖锁定文件里 ffmpeg 的 `ref`。锁定文件同时锁了 `commit` 时按 commit 拉取，**此参数不生效**；目前只对未锁 commit 的 OHOS fork 有效。换 ffmpeg 版本请改锁定文件 | 锁定文件 |
| `--enable-nvenc` | Windows 启用 `h264_nvenc`；`nv-codec-headers` 按锁定文件自动拉取（详见 1.4） | 关 |
| `--enable-amf` | Windows 启用 `h264_amf`；AMF SDK 按锁定文件自动拉取，稀疏检出（详见 1.4） | 关 |
| `--lto` | ffmpeg 开启 LTO（MSVC 下易踩坑） | 关 |

其它：`clean --all` 同时删除输出目录里对应的产物；`bootstrap --install` 自动补齐工具（各平台能力见第 6 节）；
`export -o <产物根目录>` 重新生成 CMake 包配置（`build` 结束时会自动生成，见第 7 节）；
全局 `-q/--quiet` 减少输出。

## 4. 目录结构（脚本入库，源码/产物不入库）

```
FfmpegCompileShell/
├── build.py                 CLI 入口
├── config/                  依赖锁定文件：capture_deps.json（开播，默认）/ player_deps.json（播放）
├── ffmpeg_build/
│   ├── config.py            加载锁定文件、ffmpeg 裁剪白名单、产物布局、路径、BuildContext
│   ├── deps_env.py          解析 --profile / --config-dir / --deps 与对应环境变量
│   ├── utils.py             日志 / 命令执行 / 下载解压 / 源码获取（含浅克隆/稀疏检出）/ 文件工具
│   ├── toolchains.py        Windows(MSVC) / Linux(gcc/clang) / Android(NDK) / iOS(Xcode) / OHOS(OpenHarmony SDK)
│   ├── recipes.py           各库编译配方（一库一函数）+ 硬编 SDK 的拉取与头文件落位
│   ├── bootstrap.py         工具链自检与自动补齐
│   ├── cmake/               TGCapture3rdConfig.cmake（CMake 包配置模板，build / export 时写到产物根目录）
│   └── cli.py               list / bootstrap / fetch / build / clean / export
├── scripts/                 一键入口：build_windows.ps1 / build_android.sh / build_ios.sh（Linux、OHOS 没有）
└── tests/                   冒烟测试（python -m unittest discover -s tests -t .）
```

`sources/`、`build/`、`.cache/`、`tools/` 均在 `.gitignore` 中 —— **提交时只有脚本**。

扩展方式：
* 加库：
  1. 在锁定文件里加条目（选对组：`ffmpeg_deps` / `standalone`）；
  2. 在 `config.py` 的 `_LIBRARY_SUMMARY` 加一行说明（漏了这一步，库会被**悄悄忽略**），
     并放进 `_ALL_BUILD_ORDER`（默认构建）或 `_ALL_OPTIONAL`（可选）；
  3. 在 `recipes.py` 加 `build_xxx(ctx, abi)` 并在 `REGISTRY` 注册；需要特殊目录布局时登记 `PLATFORM_LAYOUT`。
* 加平台 → `toolchains.py` 继承 `Toolchain` 并在 `TOOLCHAINS` 注册

## 5. ffmpeg 裁剪策略

开播是窄链路，采用 `--disable-everything --disable-autodetect` + 白名单，而非全量。
白名单定义在 `config.py` 的 `CAPTURE_WHITELIST` / `PLAYER_WHITELIST`，`python build.py list [--profile player]` 会打印出来。

capture（开播）实际 enable 的组件：

| 类别 | 内容 |
| - | - |
| encoder | `libopenh264,libfdk_aac,aac` + 平台编码器（Android `h264_mediacodec`、iOS `h264_videotoolbox`、Windows 开开关后 `h264_nvenc` / `h264_amf`；Linux / OHOS 不追加） |
| decoder | `h264,aac,png` |
| muxer | `flv,mpegts` |
| demuxer | `mov,flv,image2,image_png_pipe`（`mov` 同时处理 mp4/m4a；`image2` / `image_png_pipe` 用来加载水印 PNG） |
| parser | `h264,aac` |
| bsf | `h264_mp4toannexb,aac_adtstoasc,null`，另自动并入 `extract_extradata` |
| filter | `CAPTURE_FILTERS`，共 85 个（缩放、格式转换、叠加、裁剪、帧率、混音、响度等）；GPL-only 的滤镜（如 `eq` / `delogo` / `boxblur` / `perspective`）不含 |
| protocol | `file,tcp,tls,http,https,rtmp,rtmps,libsrt` |

> 所有名字在 configure 前都会对照 ffmpeg 源码的登记表校验（见 1.2），写错名字会直接报错。

其余开关：

* 关闭 `--disable-avdevice --disable-postproc --disable-vulkan --disable-iconv --disable-debug`
  和文档（`--disable-doc` 及 html/man/pod/txt pages）
  （**保留 programs**：`ffmpeg` / `ffprobe` CLI 用于验证产物可用性，它们是独立可执行文件，不进入 SDK 链接）
* `--enable-small --enable-pic --enable-nonfree --enable-version3`（fdk-aac 需要 nonfree）；`--lto` 时加 `--enable-lto`
* openssl 按 RTMPS 场景裁剪：`no-shared no-apps no-tests no-docs no-async no-dso no-ssl3 no-weak-ssl-ciphers no-comp no-dtls no-psk no-srtp`
* 非 MSVC 平台加 `-ffunction-sections -fdata-sections`：产物是静态库，脚本自身不做 `--gc-sections`，
  由消费方最终链接时加 `-Wl,--gc-sections` 剔除无用代码

各平台的 ffmpeg 差异：

| 平台 | 形态 | 平台相关开关 |
| - | - | - |
| windows | 动态（`--enable-shared --disable-static`，dll + import lib） | `--toolchain=msvc --target-os=win64 --arch=x86_64`；额外链接 `ws2_32 gdi32 crypt32 advapi32 user32`；硬编开关见 1.4 |
| linux | 静态 + `--disable-symver` | 本机编译；无 nasm 时 `--disable-x86asm`；额外链接 `m pthread dl stdc++` |
| android | 静态 + `--disable-symver` | `--enable-cross-compile --enable-jni --enable-mediacodec`，NDK clang + sysroot |
| ios | 静态 + `--disable-symver`，lipo 合并 | `--enable-cross-compile --disable-asm --enable-videotoolbox`，`-miphoneos-version-min` / `-mios-simulator-version-min` |
| ohos | 静态 + `--disable-symver` | `--enable-cross-compile --disable-x86asm`，`arm64-v8a` 加 `--enable-neon`；fork 源码 + `--enable-ohosavcodecdecoder`（见 3.4） |

* 开硬编时要记得**补回被 `--disable-everything` 一起关掉的东西**：外部库 `ffnvcodec`
  （否则 `ERROR: nvenc requested, but not all dependencies are satisfied: ffnvcodec`）与
  hwaccel `d3d11va`（AMF 内部走 D3D11）；这两个由锁定文件的 `extra_flags` 自动带上（见 1.4）

参考体积（Windows x64，本次实测，含 `h264_nvenc` + `h264_amf` 与全部开播组件）：
`avcodec-60.dll 2.61MB`、`avformat-60.dll 6.03MB`、`avutil-58.dll 4.92MB`、
`swscale 0.59MB`、`avfilter 1.08MB`、`swresample 0.21MB`。
两个硬编编码器只是"壳 + 动态加载"（真正的编解码在显卡驱动里），对 avcodec 的体积增量很小。

## 6. 环境要求与排错

| 平台 | 编译主机 | 工具链 | `bootstrap --install` 能做什么 |
| - | - | - | - |
| windows | Windows | VS 2022 Build Tools（MSVC；x86 / arm64 各需对应组件）+ MSYS2（make/install/perl/nasm）+ cmake/ninja；arm64 汇编另需 gas-preprocessor.pl（可选） | winget 装 VS Build Tools（含 x86/x64 与 ARM64 组件，失败则下载安装器并打印命令）；Strawberry Perl / nasm / cmake 便携版下到 `tools/`；make 需自装 MSYS2 |
| linux | Linux | gcc/clang + GNU make + cmake + pkg-config + perl（nasm 可选，缺则关 x86 汇编） | 只打印 dnf / apt 安装命令 |
| android | macOS / Linux | Android NDK（推荐 r27c，不校验版本）+ cmake + make + perl + pkg-config | 自动下载 NDK r27c；macOS 上 brew 装 perl / pkg-config / cmake / make，Linux 上只打印 apt / dnf 命令 |
| ios | macOS | Xcode 命令行工具 + cmake + pkg-config（ffmpeg configure 找 openssl/srt/fdk-aac/openh264 用）；nasm 只在编 `x86_64` 模拟器时需要 | `xcode-select --install` + brew 装 cmake / nasm / pkg-config |
| ohos | Linux / macOS | DevEco Studio 或 commandline-tools 的 OHOS SDK（`$OHOS_SDK`）+ cmake + make + perl | 只给出指引 |

OHOS SDK 无法自动下载（需 DevEco Studio / commandline-tools），`bootstrap --platform ohos` 只做检测并给出指引。
`build` 开始前会自动跑一遍同样的自检（只检查、不安装），可用 `--no-check` 跳过。

常见问题：

* **缺 MSVC / Perl** → `python build.py bootstrap --platform windows --install`；
  需要管理员授权的步骤会只下载到 `tools/` 并打印命令，不会卡住脚本。
* **`make: cl: No such file or directory`** → 脚本已处理（MSYS 登录 shell 会重排 PATH，
  脚本内把 MSVC 路径以 `/c/...` 形式置顶）。若仍出现，检查 MSYS2 是否安装。
* **链接报 `__imp_xxx` 未解析** → CRT 混用；脚本已统一 `/MT`，不要单独改某个库的 CRT（见 6.1）。
* **`xxx not found using pkg-config`** → 脚本已处理（shell 内 export `PKG_CONFIG_PATH`、
  `.pc` 的 prefix 改写、openssl/srt 的库名与 .pc 归一化）。
  若报的是 **命令本身不存在**（macOS 默认没有 `pkg-config`），先 `brew install pkg-config`
  再重跑；`bootstrap --platform ios|android --install` 会自动装（Android 在 Linux 宿主上只打印 apt / dnf 命令）。
* **ffmpeg 装到了奇怪目录** → 脚本已处理（Windows 路径转 `/d/...` 传给 bash）。
* **`amfenc.h(22): fatal error C1083: 无法打开包括文件 "AMF/core/Factory.h"`** →
  Windows 下 `-I` 必须写成 `D:/...` 正斜杠：`make` 经 MSYS bash 执行，配方里的
  `-ID:\a\b` 会被 shell 当转义吃掉（实测变成 `-ID:ab`），cl.exe 自然找不到头文件。
  脚本已统一按正斜杠输出（其余库以前没暴露，是因为 pkg-config 另给了一份正斜杠路径，
  AMF 没有 `.pc` 才踩到）。
* **`ffmpeg -version` 的 `configuration` 串与实际能力不一致**（例如 DLL 里已有 `h264_amf`，
  但 configuration 里没有 `--enable-amf`）→ 这两个字符串来自**不同的二进制**：
  编码器列表在 `avcodec-60.dll`，configuration 串在 `ffmpeg.exe`。MSVC 下 ffmpeg 的
  `.d` 依赖文件常常是 0 字节（cl 的 `-showIncludes` 输出被中文本地化，ffbuild 只认英文
  `Note: including file:`），make 感知不到 `config.h` 变化 → **只重编了一部分**。
  脚本已用"configure 命令行指纹"兜底（参数变了自动全量重编，见 `recipes.py::_ensure_build_dir`），
  构建结束也会体检并告警；要彻底修好增量依赖，给 VS 装 English Language Pack。
  已产出的旧产物用 `--clean` 重编一次即可对齐。
* **硬编 SDK 拉取慢** → AMF 上游仓库带约 490MB 的 `Thirdparty/`；锁定文件已配
  `--depth 1` + `--filter=blob:none` + sparse-checkout（只取 `amf/public/include`，实测 1.45MB）。
  若自建镜像不支持 `--filter`，脚本会自动退回普通浅克隆并打印告警。
* 每个阶段的完整输出在 `build/<profile>-logs/` 下，追加写入：CMake 构建的库是 `<lib>-<abi>-{cmake,build,install}.log`，
  openh264 / ffmpeg 是 `<lib>-<abi>.log`，openssl 在 Windows 上是 `openssl-{configure,build,install}.log`、其它平台是 `openssl-<abi>.log`。
  中间产物在 `build/<profile>-<platform>-<abi>/<lib>/`。

### 6.1 Windows CRT 必须一致（`/MT`）

本工程在 Windows 上默认**全部按静态 CRT（`/MT`，Release）**构建，产物只适用于同样用 `/MT`（Debug 配置为 `/MTd`）的消费方。
若消费方（如 MediaX 的某个模块 / VS 工程）是 `/MD`，链接时会出现 `__imp_xxx` 未解析或运行期堆损坏（跨 CRT 释放内存）。

各库如何固定 CRT：

| 库 | 做法 | 位置 |
| - | - | - |
| zlib / srt / fdk-aac / libyuv / spdlog(可选) | CMake：`-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded` + `CMAKE_C(XX)_FLAGS_RELEASE=/MT ...`（`--crt md` 时改为 `/MD`） | `toolchains.py::WindowsToolchain.cmake_args()` |
| openh264 | 自带 Makefile `OS=msvc`，默认 `/MT`（不受 `--crt` 控制） | `recipes.py::build_openh264()` |
| openssl | `Configure VC-WIN64A`（nmake 默认静态 CRT；所有 ABI 都用这个 target） | `recipes.py::build_openssl()` |
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

若坚持整条链路用 `/MD`：`--crt md` 只覆盖 CMake 构建的库，openh264 / openssl / ffmpeg 还得另改配方后一并重编；
只改其中某一个库必然出问题。

补充（spdlog）：产物是**编译型静态库**，链接方还需带 `SPDLOG_COMPILED_LIB`
（用 `find_package(spdlog)` 得到的 `spdlog::spdlog` target 会自动传播该宏；
手写 `-I.../include -lspdlog` 时必须自己加上，否则符号解析/ODR 出错）。

## 7. 与 MediaX 工程对接

### 7.0 CMake 工程：`find_package(TGCapture3rd)`（推荐）

产物根目录（`-o` 指定的目录）本身就是一个 **CMake 包前缀**。每次 `build` 结束都会在根目录写出
`TGCapture3rdConfig.cmake` + `TGCapture3rdConfigVersion.cmake`；旧产物没有的话可以补一份：
`python build.py export -o <产物根目录>`。消费方按 CMake 的标准用法接入，不需要手写 include/lib 目录和链接顺序：

```cmake
# cmake -DCMAKE_PREFIX_PATH=<产物根目录> ...   （或 -DTGCapture3rd_DIR=<产物根目录>）
find_package(TGCapture3rd 1 CONFIG REQUIRED COMPONENTS ffmpeg libyuv)
target_link_libraries(app PRIVATE TGCapture3rd::avformat TGCapture3rd::avcodec TGCapture3rd::libyuv)

# Windows：把 ffmpeg 的 DLL 拷到 exe 旁（CMake ≥ 3.21）
add_custom_command(TARGET app POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_RUNTIME_DLLS:app> $<TARGET_FILE_DIR:app>
    COMMAND_EXPAND_LISTS)
```

| 组件 | 导入目标 | 说明 |
| - | - | - |
| `ffmpeg` | `TGCapture3rd::avformat` / `avcodec` / `avfilter` / `swscale` / `swresample` / `avutil`，汇总目标 `TGCapture3rd::ffmpeg` | Windows 为 `SHARED IMPORTED`（`.lib` + DLL）；其它平台为静态库，openssl / srt / fdk-aac / openh264 / zlib 与系统库按 ffmpeg 自己的 `.pc` 自动传递（`.pc` 里旧的绝对 `-L` 路径会被忽略，产物整体挪位置也能用） |
| `openssl` | `TGCapture3rd::ssl` / `TGCapture3rd::crypto`，汇总 `TGCapture3rd::openssl` | 静态库，Windows 自带 ws2_32 / crypt32 等系统库 |
| `zlib` `srt` `fdk-aac` `openh264` `libyuv` `spdlog` `json` | `TGCapture3rd::<组件名>` | 静态库；`spdlog` 带 `SPDLOG_COMPILED_LIB`，`json` 为 header-only |
| `freetype` | `TGCapture3rd::freetype` | 文字场景（`--with-text`）：静态库，头文件目录 `…/include/freetype2`；不含该组件时 `TGCapture3rd_freetype_FOUND=0`（见 [1.5](#15-两种依赖场景现有依赖--带字符集字形库)） |

* **平台 / ABI 自动推断**：Windows 按编译器目标架构（`-A x64|Win32|ARM64`），Android 按 `ANDROID_ABI`，
  OHOS 按 `OHOS_ARCH`，Linux 按 `CMAKE_SYSTEM_PROCESSOR`，iOS 用 fat 库；也可以用 `-DTGCapture3rd_PLATFORM=` / `-DTGCapture3rd_ABI=` 指定。
* **架构核对**：Windows 的 flat 布局没有 abi 层，包配置会读版本标记里的 `platform : windows (x64)` 核对，
  比如 `-A Win32` 却指向 x64 产物时，在 configure 阶段就报错，而不是到链接时才报 `LNK1112`。
* **交叉编译**：Android / OHOS 工具链把包查找限定在 sysroot（`CMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY`），
  `find_package` 要加 `NO_CMAKE_FIND_ROOT_PATH`，或直接给 `-DTGCapture3rd_DIR=<产物根目录>`。
* 输出变量：`TGCapture3rd_<组件>_FOUND` / `_VERSION` / `_PREFIX`、`TGCapture3rd_PROFILE`（来自 `.build_profile`）、
  `TGCapture3rd_RUNTIME_DLLS`（Windows ffmpeg DLL 列表）。在 `add_subdirectory` 引入的子工程里查找、
  上层也要用这些目标时，设 `TGCapture3rd_IMPORT_GLOBAL=ON`（或 CMake 3.24+ 的 `CMAKE_FIND_PACKAGE_TARGETS_GLOBAL`）。
* 包版本 `1.x`（导入目标命名与组件划分的接口版本，与各库版本无关），主版本不同视为不兼容。
* 用法示例见 `RefactorMediaX/cmake/Deps.cmake`。

### 7.1 拷贝到 `MediaX/ESMediaX/3rd`
> 说明：`MediaX/ESMediaX/3rd` 原有的构建脚本（`build_ffmpeg.py`、`build_fdkaac.py`、`build_openh264.py`、
> `build_spdlog.py`、`public.py`、`ohos_envset.sh`）已并入本工程并删除，该仓库现在只存放预编译产物。

产物目录结构与 `MediaX/ESMediaX/3rd` 一致，替换时：

```powershell
# 例：更新 Windows 开播使用的 ffmpeg 与 openh264（源目录即 -o 指定的输出目录）
Copy-Item D:\work\TGCapture3rd\ffmpeg\windows\*   MediaX\ESMediaX\3rd\ffmpeg-capturer\windows\ -Recurse -Force
```

Android 的 libyuv 会额外产出 `libyuv_static.a`（兼容既有链接配置）；
Windows 的 ffmpeg 同时提供 `lib/*.lib`（import lib）与 `bin/*.dll`。

对接时注意几个硬约束：

* **CRT**：Windows 产物全是 `/MT`，MediaX 侧对应模块必须同为 `/MT`（详见 6.1）。
* **spdlog**：MediaX 的 `ESLogger` 依赖编译型 spdlog，版本锁 `v1.14.1`，且需 `SPDLOG_COMPILED_LIB`（详见 6.1）。
* **硬编（Windows）**：新版 `avcodec-60.dll` 已带 `h264_nvenc` / `h264_amf`；之前
  `ESVideoEncoder.cpp` 找不到硬编会**静默回退** `libopenh264`，现在上层可以显式选硬编
  （拿不到硬编时仍走软编回退）。运行期不需要随包任何 SDK 文件（编解码由显卡驱动提供），
  因此**不要**把 `amf/` 或 `nv-codec-headers/` 拷进 `3rd`（详见 1.4）。

### 7.2 cuda-samples（NPP）：现状与将来建议

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

