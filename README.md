# FfmpegCompileShell

编译 **ffmpeg 及其依赖库** 的跨平台脚本（Python 3.8+，仅用标准库，零第三方依赖）。
产物默认输出到工程同级的 `TGCapture3rd`，供开播工程直接取用。

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

不使用 x264 / x265 / v265；AMF、nv-codec-headers、zlib、cuda-samples(NPP) 经复核均非开播编译期依赖，故不纳入。

## 2. 产物输出目录（默认 TGCapture3rd，可配置）

三种方式，优先级从高到低：

```bash
# 1) 命令行（推荐，最直观）
python build.py build --platform windows -o D:/work/TGCapture3rd

# 2) 环境变量（适合 CI）
export FFMPEG_3RD_OUTPUT=D:/work/TGCapture3rd

# 3) 默认值：工程同级目录/TGCapture3rd
#    FfmpegCompileShell/../TGCapture3rd
```

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

每个库目录下有 `version.txt`（版本、平台、构建时间、源码路径），便于追溯。

## 3. 使用攻略

### 3.1 首次使用（Windows）

```powershell
# ① 检查并补齐工具链（缺 MSVC / 原生 Perl 会自动下载；winget 可用时自动静默安装）
python build.py bootstrap --platform windows --install

# ②（可选）只拉源码，确认内网/外网可达
python build.py fetch --platform windows

# ③ 编译全部依赖 -> ../TGCapture3rd
python build.py build --platform windows

# ④ 查看产物
python build.py list
dir ..\TGCapture3rd
```

一键脚本：`powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1`

### 3.2 macOS 上编译 iOS / Android

```bash
python3 build.py bootstrap --platform ios --install
python3 build.py build --platform ios                 # arm64 + x86_64 -> lipo 合并

python3 build.py bootstrap --platform android --install
python3 build.py build --platform android --abi arm64-v8a --api 21
```

脚本：`./scripts/build_ios.sh`、`./scripts/build_android.sh`

### 3.3 分步 / 增量

```bash
python build.py build --platform windows --libs openssl      # 只编一个库
python build.py build --platform windows --libs ffmpeg        # 依赖已就绪时单独重编 ffmpeg
python build.py build --platform windows --clean              # 清理后全量重建
python build.py clean --platform windows --all                # 只清理（含产物）
python build.py build --platform windows -j 8                 # 指定并发
```

> ffmpeg 构建前会校验 `openssl / fdk-aac / openh264 / srt` 是否已产出，
> 未就绪会直接提示"请先编译依赖"，而不是报含糊的 `xxx not found`。

### 3.4 常用参数

| 参数 | 说明 | 默认 |
| - | - | - |
| `-p/--platform` | `windows` / `android` / `ios` | 必填 |
| `--abi` | windows `x64`；android `arm64-v8a`/`armeabi-v7a`/`x86_64`；ios `arm64`/`x86_64`/`fat` | 平台默认 |
| `--libs` | 逗号分隔，`all` 为必须库全集 | `all` |
| `-o/--output` | 产物根目录 | `../TGCapture3rd` |
| `-j/--jobs` | 并行任务数 | CPU-1 |
| `--toolchain` | Windows 工具链偏好 `auto`/`msvc`/`mingw` | `auto`（优先 MSVC） |
| `--ffmpeg-ref` | ffmpeg 仓库分支/标签 | `master` |
| `--enable-nvenc` | Windows 启用 NVENC（需自备 nv-codec-headers） | 关 |
| `--lto` | ffmpeg 开启 LTO（MSVC 下易踩坑） | 关 |
| `--api` / `--min-version` | Android API / iOS 最低版本 | 21 / 12.0 |
| `--force-fetch` | 强制重新拉取源码 | 关 |
| `--dry-run` | 只打印命令不执行 | 关 |

## 4. 目录结构（脚本入库，源码/产物不入库）

```
FfmpegCompileShell/
├── build.py                 CLI 入口
├── ffmpeg_build/
│   ├── config.py            依赖清单、ffmpeg 裁剪白名单、路径、BuildContext
│   ├── utils.py             日志 / 命令执行 / 下载解压 / 源码获取 / 文件工具
│   ├── toolchains.py        Windows(MSVC) / Android(NDK) / iOS(Xcode)
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
* 关闭 `--disable-avdevice --disable-postproc --disable-programs --disable-doc --disable-iconv --disable-vulkan`
* `--enable-small`；Windows 用 shared（dll + import lib），Android/iOS 用 static（`--disable-symver`）
* openssl 按 RTMPS 场景裁剪：`no-apps no-tests no-docs no-ssl3 no-weak-ssl-ciphers no-comp no-dtls no-psk no-srtp`
* 非 MSVC 编译加 `-ffunction-sections -fdata-sections`，最终链接 .so 时配 `-Wl,--gc-sections`

参考体积（Windows x64）：`avcodec-60.dll 2.49MB`、`avformat-60.dll 4.82MB`、`avutil-58.dll 4.41MB`、
`swscale 0.59MB`、`avfilter 0.29MB`、`swresample 0.21MB`。

## 6. 环境要求与排错

| 平台 | 编译主机 | 工具链 |
| - | - | - |
| windows | Windows | VS 2022 Build Tools（MSVC）+ MSYS2（make/install/perl/nasm）+ cmake/ninja |
| android | macOS / Linux | Android NDK r21+（可 `bootstrap --install` 自动下载 r27c）+ cmake + make |
| ios | macOS | Xcode 命令行工具 + cmake + nasm（模拟器架构需要） |

常见问题：

* **缺 MSVC / Perl** → `python build.py bootstrap --platform windows --install`；
  需要管理员授权的步骤会只下载到 `tools/` 并打印命令，不会卡住脚本。
* **`make: cl: No such file or directory`** → 脚本已处理（MSYS 登录 shell 会重排 PATH，
  脚本内把 MSVC 路径以 `/c/...` 形式置顶）。若仍出现，检查 MSYS2 是否安装。
* **链接报 `__imp_xxx` 未解析** → CRT 混用；脚本已统一 `/MT`，不要单独改某个库的 CRT。
* **`xxx not found using pkg-config`** → 脚本已处理（shell 内 export `PKG_CONFIG_PATH`、
  `.pc` 的 prefix 改写、openssl/srt 的库名与 .pc 归一化）。
* **ffmpeg 装到了奇怪目录** → 脚本已处理（Windows 路径转 `/d/...` 传给 bash）。
* 每个阶段的完整输出在 `build/logs/<lib>-<abi>-<stage>.log`。

## 7. 与 MediaX 工程对接

产物目录结构与 `MediaX/ESMediaX/3rd` 一致，替换时：

```powershell
# 例：更新 Windows 开播使用的 ffmpeg 与 openh264
Copy-Item ..\TGCapture3rd\ffmpeg\windows\x64\*   MediaX\ESMediaX\3rd\ffmpeg-capturer\windows\ -Recurse -Force
```

Android 的 libyuv 会额外产出 `libyuv_static.a`（兼容既有链接配置）；
Windows 的 ffmpeg 同时提供 `lib/*.lib`（import lib）与 `bin/*.dll`。
