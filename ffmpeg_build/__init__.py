"""FfmpegCompileShell：ffmpeg 及其依赖库的跨平台编译工具。

设计目标：脚本入 git、源码与产物不入库；结构扁平（config/utils/toolchains/recipes/
bootstrap/cli 六个模块），新增一个库只改 config.py + recipes.py 两处。
"""

__version__ = "1.0.0"
