# FfmpegCompileShell

编译 **ffmpeg 及其依赖库** 的跨平台脚本（Python 3.8+，仅用标准库，零第三方依赖）。
开播（capture）/ 播放（player）两套 profile；Windows 可选编入桌面硬编 `h264_nvenc` / `h264_amf`
（SDK 按锁定文件自动从上游拉取，见 [1.4](#14-windows-硬编nvenc--amf)）。

## 目录

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

不使用 x264 / x265 / v265；cuda-samples(NPP) 经复核非开播编译期依赖，不纳入。

**AMF 与 nv-codec-headers** 是 Windows 硬编（`h264_amf` / `h264_nvenc`）的**构建期输入**（只要头文件，
不编译、运行期靠显卡驱动），默认不参与构建，需要时按 [1.4](#14-windows-硬编nvenc--amf) 打开开关。

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

分四组（与 ffmpeg 的耦合关系）：

| 组 | 含义 | 库 |
| - | - | - |
| `ffmpeg` | FFmpeg 自身源码，按**用途(profile)**再分档 | `ffmpeg`（capture 文件里 profile=**capture**；player 文件里 profile=**player**） |
| `ffmpeg_deps` | FFmpeg 的**构建期依赖**，需对应 `--enable-*`，产物静态并入 avcodec/avformat | `openssl`、`srt`、`fdk-aac`、`openh264` |
| `standalone` | 与 FFmpeg **无关**的独立库，应用直接链接 | `libyuv`、`json`、`spdlog` |
| `ffmpeg_hw_sdks` | 桌面**硬编 SDK**：FFmpeg 的构建期输入（头文件，不编译、不随产物分发） | `amf`、`nv-codec-headers`（仅 Windows + 显式开关，见 1.4） |

规则：

* **内网仓库**（`EST/media/e-ffmpeg`）：`ffmpeg`（ffmpeg-6.1.6）、`fdk-aac`（fdk_aac_v2.0.3）。
* **硬编 SDK 用上游官方仓库并锁 tag**：`amf` → `GPUOpen-LibrariesAndSDKs/AMF` `v1.4.34`、
  `nv-codec-headers` → `FFmpeg/nv-codec-headers` `n12.0.16.1`（两者都同时锁 commit，见 1.4）。
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
| 参与构建的库 | 9 个（含 **zlib**/openssl/srt/fdk-aac/openh264/libyuv） | 4 个（**zlib** / ffmpeg / json / spdlog） |
| ffmpeg 外部库开关 | `--enable-zlib --enable-openssl --enable-libsrt --enable-libfdk-aac --enable-libopenh264`（**来自锁定文件 `ffmpeg_deps` 组的 `ffmpeg_flag`**，非硬编码）；开硬编时再追加 `--enable-ffnvcodec` / `--enable-d3d11va`（来自 `ffmpeg_hw_sdks` 组，见 1.4） | `--enable-zlib`（png 解码依赖 zlib） |
| 裁剪白名单（`config.PROFILE_WHITELIST`） | 编码器 `libopenh264,libfdk_aac,aac`（+ 平台硬编：Android `h264_mediacodec`、iOS `h264_videotoolbox`、Windows 开开关后 `h264_nvenc`/`h264_amf`）、封装 `flv,mpegts`、解封装含 `image2`（水印图）、协议 `file,tcp,tls,http,https,rtmp,rtmps,libsrt` | **不生成** `--enable-encoder` / `--enable-muxer`；解码器 `h264,hevc,aac,mp3,png,mjpeg`、解封装 `mov,mp4,flv,mkv,mpegts,hls`、协议 `file,http,tcp,hls` |

两个白名单细节（都是踩过坑才改的）：

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
* ffmpeg 的版本标记文件名带 profile：`version-ffmpeg-6.1.6-g…-…-…windows.x64.player.msvc.txt`。

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

* 作用域：**只对 CMake 构建的库生效**（`spdlog` / `libyuv` / `json` / `srt` / `fdk-aac` / `openh264`）；
  `openssl` 与 `ffmpeg` 始终 `/MT`（ffmpeg 是 DLL 且为 C 接口，`/MT` 的 DLL 被 `/MD` 的 exe 加载没问题）。
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

### 2.1 Windows 产物目录布局（对齐 `MediaX/ESMediaX/3rd`）

Windows 下按 **3rd-bak 的历史结构**输出，产物可以整目录拷进 `ESMediaX/3rd` 直接替换：

| 布局 | 结构 | 对应库 | 3rd-bak 里的样例 |
| - | - | - | - |
| `flat` | `<库>/windows/{include,lib,bin}`（**无 abi 层**） | `ffmpeg` `libyuv` `zlib` `openssl` `srt` `fdk-aac` `openh264` | `ffmpeg-capturer/windows/{bin,include,lib}`、`libyuv/windows/{bin,include,lib}` |
| `lib` | `<库>/windows/include` + `<库>/windows/lib/<abi>` | `spdlog` | `spdlog/windows/lib/x64`、`spdlog/windows/lib/x86` |
| `abi` | `<库>/windows/<abi>/{include,lib,bin}`（本平台默认） | 未登记的库；以及**同时编多个 ABI** 时自动回落 | — |

Android / iOS / OHOS 同样以 `PLATFORM_LAYOUT` 对齐 `3rd-bak`：

| 平台 | 布局 | 说明 |
| - | - | - |
| linux | `abi`：`<库>/linux/<abi>/{include,lib,bin}` | 无 3rd-bak 历史包袱，统一带 abi 层 |
| android | `abi`（默认）：`<库>/android/<abi>/{include,lib,bin}`；`spdlog` → `flat` | 对齐 `ffmpeg-capturer/android/arm64-v8a/{bin,include,lib,share}`、`spdlog/android/{include,lib}` |
| ohos | 同上（`libyuv/ohos/<abi>`、`spdlog/ohos/{include,lib}`） | 对齐 3rd-bak |
| ios | `flat` + lipo 合并：**`<库>/ios/{include,lib}`（fat，无 abi 层）** | 对齐 `ffmpeg-capturer/ios/{include,lib}`、`libyuv/ios/{include,lib}` |

* 布局表在 `config.WINDOWS_LAYOUT`，加新库时登记一行即可。
* 硬编 SDK（`amf` / `nv-codec-headers`）同样是**无 abi 层的 flat**：`<库>/windows/{include,lib}`，
  且只在开了 `--enable-amf` / `--enable-nvenc` 时才产出，属于构建期输入（见 1.4）。
* flat / lib 布局没有 abi 层，`--abi x64,x86` 同时编会互相覆盖 → 检测到多 ABI 时自动改用 abi 目录并打印提示。
* 其它平台（android/ohos 为 `<库>/<平台>/<abi>`，ios 为 fat 库无 abi 层）维持原样。

**portable（与平台无关的库）**：header-only 的 `json` 在锁定文件里声明 `"layout": "portable"`，
产物直接放 `<输出根>/json/…`，**不带 `<平台>`/`<abi>` 层**，三端共用一份
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
例：version-x64-ffmpeg-6.1.6-g3c1fc5d-2026-10-19-22-47.windows.x64.capture.msvc.crtmt.txt
    version-x86-spdlog-v1.14.1-g27cb4c7-2026-10-20-09-05.windows.x86.msvc.crtmd.txt
    version-arm64-v8a-ffmpeg-6.1.6-g3c1fc5d-…android.arm64-v8a.capture.ndk-clang.api21.txt
    version-ffmpeg-6.1.6-g3c1fc5d-….ios.capture.xcode-clang.min13.0.txt      （iOS fat 无 abi 前缀）
    version-json-3.12.0-2026-10-20-11-56.any.header-only.txt                 （portable 库）
```

**abi 放在最前面**，一眼分清 x64 / x86 / arm64-v8a；末尾的**关键信息**按平台记最要紧的构建约束
（与 `3rd-bak` 里 `ffmpeg-6.0-…-min13.0.txt` 的做法一致）：

| 平台 | 关键信息 | 来源 |
| - | - | - |
| windows | `crtmt` / `crtmd` | `--crt` |
| android | `api21`（有 NDK 版本时再带 `-ndk…`） | `--android-api` / NDK |
| ios | `min13.0` | `--min-version` |
| portable(header-only) | 省略 | — |

文件里另有 `profile` / `group` / `crt` / `variant` / `layout` 等行。

产物布局（与 `MediaX/ESMediaX/3rd/<库>/<平台>/<abi>` 一致，可直接替换）：

```
TGCapture3rd/
├── openssl/windows/{include,lib}
├── srt/windows/{include,lib}
├── fdk-aac/windows/{include,lib}
├── openh264/windows/{include,lib}
├── libyuv/windows/{include,lib}
├── ffmpeg/windows/{include,lib,bin}
├── amf/windows/include/AMF/...                # 仅 --enable-amf：构建期输入
├── nv-codec-headers/windows/{include,lib}     # 仅 --enable-nvenc：构建期输入
├── */android/<abi>/...
└── */ios/...                                  # iOS 为 fat 库，不带 abi 层
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

# ③' Windows 开播要硬编时（可选）：加 --enable-nvenc / --enable-amf，
#     SDK 会按锁定文件自动从上游拉取，不用自备（详见 1.4）
python build.py build --platform windows -o D:/work/TGCapture3rd --enable-nvenc --enable-amf

# ④ 查看产物
python build.py list
dir D:\work\TGCapture3rd
```

> 默认（不加开关）只编软编 `libopenh264`，产物最小；硬编 SDK 需要联网拉取，
> 且只在 Windows 生效 —— iOS/Android 用 `h264_videotoolbox` / `h264_mediacodec`，无需 SDK。

一键脚本：`powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1`

### 3.2 Linux（本机 gcc）

服务端开播 / 本机验证：**本机编译，不需要任何交叉工具链**（gcc + make + cmake +
pkg-config + perl；nasm 可选）。

```bash
# ① 自检（缺什么就直接给出 dnf/apt 安装命令；nasm 缺失只是降级，不阻断）
python3 build.py bootstrap --platform linux

# ② 编译开播全套 -> <输出目录>/<库>/linux/x86_64
python3 build.py build --platform linux -o ~/TGCapture3rd-linux

# 只编 ffmpeg（依赖已就绪时）
python3 build.py build --platform linux -o ~/TGCapture3rd-linux --libs ffmpeg
```

要点：

* **ABI 默认取宿主架构**（`x86_64` / `arm64` / `armhf`），不必显式给 `--abi`；`--abi all` 三个架构全编。
* 产物是**静态库**（`--enable-static --disable-shared`，与 Android/iOS 一致）。
  `ffmpeg/linux/x86_64/bin/ffmpeg` 可直接执行，构建结束会**自动跑一次组件自检**
  （白名单里的编码器 / 协议 / 滤镜是否真编进去了，缺了会 WARN）——
  这是本机平台独有的好处，Windows 之外终于也能自检了。
* **x86 汇编需要 nasm**；没有时自动 `--disable-x86asm`（openh264 用 `USE_ASM=No`），
  不会编译到一半才失败。
* **内网仓库不可达时的兜底**：`ffmpeg` 与 `fdk-aac` 在锁定文件里带 `platform_overrides.linux`，
  改用上游 GitHub 的同版本（ffmpeg → `n6.1.6`；fdk-aac → `v2.0.3`，其 commit 与内网版完全相同）。
  这是**通用的按平台换源**能力（任何库都能写 `platform_overrides`），其它平台仍走内网。
* **RHEL 系发行版**（TencentOS / CentOS / Rocky）上 CMake 与 openssl 默认装到 `lib64`，
  而 ffmpeg 的 configure 固定按 `<prefix>/lib` 找依赖 —— 脚本会在安装后把 `lib64`
  收敛到 `lib`，产物布局与其它平台一致（见 `recipes.py::collapse_lib_dirs`）。

### 3.3 macOS 上编译 iOS / Android

```bash
python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios -o D:/work/TGCapture3rd          # arm64 + x86_64 -> lipo 合并

python3 build.py bootstrap --platform android --install
python3 build.py build --platform android --abi arm64-v8a --api 21 -o D:/work/TGCapture3rd
```

脚本（输出目录同样必填：第二个参数，或环境变量 `FFMPEG_3RD_OUTPUT`，不给时默认 `../TGCapture3rd`）：
`./scripts/build_ios.sh fat /path/to/TGCapture3rd`、`./scripts/build_android.sh`

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

* ABI：`arm64-v8a` / `armeabi-v7a` / `x86_64`，默认 `arm64-v8a`。
* cmake 优先用 SDK 自带的 `native/build-tools/cmake/bin/cmake`，并注入
  `-DOHOS_PLATFORM=OHOS -DOHOS_ARCH=<abi> -DCMAKE_TOOLCHAIN_FILE=<sdk>/native/build/cmake/ohos.toolchain.cmake`。
* openssl 走 `linux-aarch64` / `linux-armv4` / `linux-x86_64` target；openh264 走 `make OS=linux ARCH=<arch>`。
* ffmpeg 默认改用 openharmony-tpc 的 fork（`ohos-n6.1.2_dev`），并追加 `--enable-ohosavcodecdecoder`；
  若改用官方 ffmpeg 源码，把 `config.py` 的 `OHOS_FFMPEG_EXTRA_ARGS` 置空即可。
* 宿主建议 Linux/macOS；Windows 需自备 MSYS2/Git Bash（提供 bash + GNU make + perl）。

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

> ffmpeg 构建前会校验 `openssl / fdk-aac / openh264 / srt` 是否已产出，
> 未就绪会直接提示"请先编译依赖"，而不是报含糊的 `xxx not found`。

### 3.6 常用参数

| 参数 | 说明 | 默认 |
| - | - | - |
| `-p/--platform` | `windows` / `linux` / `android` / `ios` / `ohos` | 必填 |
| `--abi` | windows `x64`/`x86`/`arm64`；linux `x86_64`/`arm64`/`armhf`（默认宿主架构）；android `arm64-v8a`/`armeabi-v7a`/`x86_64`；ios `arm64`/`x86_64`/`fat`；ohos `arm64-v8a`/`armeabi-v7a`/`x86_64` | 平台默认 |
| `--libs` | 逗号分隔，`all` 为必须库全集 | `all` |
| `-o/--output` | 产物输出目录 | **必填**（或环境变量 `FFMPEG_3RD_OUTPUT`） |
| `-j/--jobs` | 并行任务数 | CPU-1 |
| `--toolchain` | Windows 工具链偏好 `auto`/`msvc`/`mingw` | `auto`（优先 MSVC） |
| `--ffmpeg-ref` | ffmpeg 仓库分支/标签（不给则用 `config.py` 中的 ref） | 平台默认 |
| `--enable-nvenc` | Windows 启用 `h264_nvenc`；`nv-codec-headers` 按锁定文件自动拉取（详见 1.4） | 关 |
| `--enable-amf` | Windows 启用 `h264_amf`；AMF SDK 按锁定文件自动拉取，稀疏检出（详见 1.4） | 关 |
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
│   ├── config.py            依赖清单、ffmpeg 裁剪白名单、硬编 SDK 定义、路径、BuildContext
│   ├── utils.py             日志 / 命令执行 / 下载解压 / 源码获取（含浅克隆/稀疏检出）/ 文件工具
│   ├── toolchains.py        Windows(MSVC) / Android(NDK) / iOS(Xcode) / OHOS(OpenHarmony SDK)
│   ├── recipes.py           各库编译配方（一库一函数）+ 硬编 SDK 的拉取与头文件落位
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

* 只 enable：encoder `libopenh264,libfdk_aac,aac`（Android 加 `h264_mediacodec`，iOS 加 `h264_videotoolbox`，
  Windows 开 `--enable-nvenc` / `--enable-amf` 后加 `h264_nvenc` / `h264_amf`）、
  decoder `h264,aac,png`、muxer `flv,mpegts`、demuxer `mov,flv,mp4`、parser `h264,aac`、
  bsf `h264_mp4toannexb,aac_adtstoasc,null`、filter `scale,format,aformat,aresample,overlay,null,anull`、
  protocol `file,tcp,tls,rtmp,rtmps,srt`
* 关闭 `--disable-avdevice --disable-postproc --disable-doc --disable-iconv --disable-vulkan`
  （**保留 programs**：`ffmpeg` / `ffprobe` CLI 用于验证产物可用性，它们是独立可执行文件，不进入 SDK 链接）
* `--enable-small`；Windows 用 shared（dll + import lib），Android/iOS 用 static（`--disable-symver`）
* openssl 按 RTMPS 场景裁剪：`no-apps no-tests no-docs no-ssl3 no-weak-ssl-ciphers no-comp no-dtls no-psk no-srtp`
* 非 MSVC 编译加 `-ffunction-sections -fdata-sections`，最终链接 .so 时配 `-Wl,--gc-sections`
* OHOS 额外追加 `--enable-ohosavcodecdecoder`（见 `config.py::OHOS_FFMPEG_EXTRA_ARGS`）

* 开硬编时要记得**补回被 `--disable-everything` 一起关掉的东西**：外部库 `ffnvcodec`
  （否则 `ERROR: nvenc requested, but not all dependencies are satisfied: ffnvcodec`）与
  hwaccel `d3d11va`（AMF 内部走 D3D11）；这两个由锁定文件的 `extra_flags` 自动带上（见 1.4）

参考体积（Windows x64，本次实测，含 `h264_nvenc` + `h264_amf` 与全部开播组件）：
`avcodec-60.dll 2.61MB`、`avformat-60.dll 6.03MB`、`avutil-58.dll 4.92MB`、
`swscale 0.59MB`、`avfilter 1.08MB`、`swresample 0.21MB`。
两个硬编编码器只是"壳 + 动态加载"（真正的编解码在显卡驱动里），对 avcodec 的体积增量很小。

## 6. 环境要求与排错

| 平台 | 编译主机 | 工具链 |
| - | - | - |
| linux | Linux | gcc/clang + GNU make + cmake + pkg-config + perl（nasm 可选，缺则关 x86 汇编） |
| windows | Windows | VS 2022 Build Tools（MSVC）+ MSYS2（make/install/perl/nasm）+ cmake/ninja |
| android | macOS / Linux | Android NDK r21+（可 `bootstrap --install` 自动下载 r27c）+ cmake + make |
| ios | macOS | Xcode 命令行工具 + cmake + nasm（模拟器架构需要）+ pkg-config（ffmpeg configure 找 openssl/srt/fdk-aac/openh264 用） |
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
  若报的是 **命令本身不存在**（macOS 默认没有 `pkg-config`），先 `brew install pkg-config`
  再重跑；`bootstrap --platform ios/android --install` 会自动装。
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
* **硬编（Windows）**：新版 `avcodec-60.dll` 已带 `h264_nvenc` / `h264_amf`；之前
  `ESVideoEncoder.cpp` 找不到硬编会**静默回退** `libopenh264`，现在上层可以显式选硬编
  （拿不到硬编时仍走软编回退）。运行期不需要随包任何 SDK 文件（编解码由显卡驱动提供），
  因此**不要**把 `amf/` 或 `nv-codec-headers/` 拷进 `3rd`（详见 1.4）。

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

