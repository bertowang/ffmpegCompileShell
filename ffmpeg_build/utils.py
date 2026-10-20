"""通用工具：日志、命令执行、下载解压、源码获取、文件与路径处理。"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

Command = Union[str, Sequence[str]]


def _tolerant_stdio() -> None:
    """Windows 控制台/重定向常是 GBK，而 nmake、cl 等工具输出可能含其无法编码的字符
    （例如 \\ufffd），直接 print 会抛 UnicodeEncodeError 让整条构建中断。
    这里统一改成"写不出来就替换"，保证构建日志不会因为一个字符而失败。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


_tolerant_stdio()


@dataclass(frozen=True)
class GitSource:
    """git 源码：``ref`` 为 tag/branch，``commit`` 优先级更高。"""

    url: str
    ref: Optional[str] = None
    commit: Optional[str] = None


@dataclass(frozen=True)
class ArchiveSource:
    """源码归档包（tar.* / zip）。"""

    url: str
    strip_components: int = 1
    sha256: Optional[str] = None


# ------------------------------------------------------------------- 日志
class Logger:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose

    def _emit(self, level: str, message: str) -> None:
        if level == "DEBUG" and not self.verbose:
            return
        print(f"{datetime.now():%H:%M:%S} {level:<5} {message}", flush=True)

    def debug(self, m: str) -> None:
        self._emit("DEBUG", m)

    def info(self, m: str) -> None:
        self._emit("INFO", m)

    def warn(self, m: str) -> None:
        self._emit("WARN", m)

    def error(self, m: str) -> None:
        self._emit("ERROR", m)

    def step(self, m: str) -> None:
        self._emit("STEP", m)

    def section(self, title: str) -> None:
        print(f"\n{'=' * (len(title) + 12)}\n===== {title} =====\n{'=' * (len(title) + 12)}", flush=True)

    def command(self, text: str) -> None:
        if self.verbose:
            print(f"        $ {text}", flush=True)


# --------------------------------------------------------------- 命令执行
class CommandResult:
    def __init__(self, returncode: int, output: str, command: str):
        self.returncode = returncode
        self.output = output
        self.command = command


class CommandRunner:
    """实时输出 + 日志落盘 + 非 0 即抛错。"""

    def __init__(self, logger: Optional[Logger] = None, dry_run: bool = False, log_dir: Optional[Path] = None):
        self.logger = logger or Logger()
        self.dry_run = dry_run
        self.log_dir = log_dir

    def run(
        self,
        cmd: Command,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        check: bool = True,
        quiet: bool = False,
        log_name: Optional[str] = None,
    ) -> CommandResult:
        printable = cmd if isinstance(cmd, str) else (
            subprocess.list2cmdline(list(cmd)) if os.name == "nt"
            else " ".join(shlex.quote(str(p)) for p in cmd)
        )
        if not quiet:
            self.logger.command(printable)
        if self.dry_run:
            return CommandResult(0, "", printable)

        log_file = None
        if self.log_dir and log_name:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = (self.log_dir / f"{log_name}.log").open("a", encoding="utf-8", errors="replace")

        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=env if env is not None else os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                shell=isinstance(cmd, str),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"命令不存在: {printable}\n  {exc}") from exc

        chunks: List[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                chunks.append(line)
                if log_file:
                    log_file.write(line)
                if not quiet:
                    print(line.rstrip(), flush=True)
            code = process.wait()
        finally:
            if log_file:
                log_file.close()

        result = CommandResult(code, "".join(chunks), printable)
        if check and code != 0:
            raise RuntimeError(
                f"命令失败(exit={code}): {printable}\n  目录: {cwd or os.getcwd()}\n"
                f"  输出尾部: {result.output.strip()[-600:]}"
            )
        return result

    def capture(self, cmd: Command, **kwargs) -> str:
        return self.run(cmd, check=False, quiet=True, **kwargs).output.strip()


# ------------------------------------------------------------------ 下载
_CHUNK = 1 << 16


def download_file(url: str, dest: Path, logger: Logger, sha256: Optional[str] = None) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and (not sha256 or _sha256(dest) == sha256):
        logger.info(f"复用已下载: {dest.name}")
        return dest

    logger.info(f"下载 {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "ffmpeg-compile-shell/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, tmp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r        {done * 100 // total:3d}%", end="", flush=True)
        print("", flush=True)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败 {url}: {exc}") from exc

    if sha256 and _sha256(tmp) != sha256:
        tmp.unlink()
        raise RuntimeError(f"{dest.name} 校验失败")
    return tmp.replace(dest)


def extract_archive(archive: Path, dest: Path, logger: Logger, strip_components: int = 0) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            roots = {n.split("/")[0] for n in zf.namelist() if "/" in n}
            root = roots.pop() if len(roots) == 1 else ""
            for member in zf.namelist():
                name = member[len(root) + 1 :] if root and member.startswith(root + "/") else member
                parts = name.split("/")[strip_components:] if strip_components else name.split("/")
                name = "/".join(parts)
                if not name:
                    continue
                target = dest / name
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                parts = member.name.split("/")[strip_components:]
                if not parts or not parts[0]:
                    continue
                member.name = "/".join(parts)
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    continue
                tf.extract(member, dest)
    logger.info(f"解压 -> {dest}")
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ 源码
class SourceManager:
    def __init__(self, runner: CommandRunner, logger: Logger, cache_dir: Path):
        self.runner = runner
        self.logger = logger
        self.cache_dir = Path(cache_dir)

    def ensure(self, spec, dest: Path, force: bool = False) -> Path:
        dest = Path(dest)
        if dest.exists() and any(dest.iterdir()) and not force:
            if _pin_matches(dest / ".deps_pin", spec):
                self.logger.info(f"复用源码: {dest}")
                return dest
            # 锁定值变了（锁定文件升级/回退），必须重新拉取才能保证可复现
            self.logger.info(f"源码与锁定文件不一致，重新拉取: {dest}")
            force = True
        if force and dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(spec, GitSource):
            self.logger.info(f"git {spec.url} @ {spec.commit or spec.ref or 'HEAD'}")
            if spec.commit:
                dest.mkdir(parents=True, exist_ok=True)
                self.runner.run(["git", "init", "-q", "."], cwd=dest)
                self.runner.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, quiet=True)
                self.runner.run(["git", "remote", "add", "origin", spec.url], cwd=dest, check=False, quiet=True)
                self.runner.run(["git", "fetch", "--depth", "1", "origin", spec.commit], cwd=dest, log_name="git-fetch")
                self.runner.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
            else:
                cmd = ["git", "clone", "--depth", "1"]
                if spec.ref:
                    cmd += ["-b", spec.ref]
                self.runner.run(cmd + [spec.url, str(dest)], log_name="git-clone")
        elif isinstance(spec, ArchiveSource):
            filename = spec.url.rsplit("/", 1)[-1].split("?")[0]
            archive = download_file(spec.url, self.cache_dir / filename, self.logger, spec.sha256)
            extract_archive(archive, dest, self.logger, spec.strip_components)
        else:
            raise RuntimeError(f"未知源码类型: {spec!r}")
        _write_pin(dest / ".deps_pin", spec)
        return dest


def _pin_of(spec) -> Dict[str, object]:
    """把源码描述压缩成可比较的锁定信息（写入源码目录，用于校验一致性）。"""
    if isinstance(spec, GitSource):
        return {"kind": "git", "url": spec.url, "ref": spec.ref, "commit": spec.commit}
    if isinstance(spec, ArchiveSource):
        return {"kind": "archive", "url": spec.url, "sha256": spec.sha256,
                "strip_components": spec.strip_components}
    return {"kind": "unknown"}


def _write_pin(marker: Path, spec) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(_pin_of(spec), sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _pin_matches(marker: Path, spec) -> bool:
    """源码目录的锁定标记与当前 spec 一致才允许复用。"""
    if not marker.exists():
        return False
    try:
        return json.loads(marker.read_text(encoding="utf-8")) == _pin_of(spec)
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ 杂项
def which(name: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    search = (env or {}).get("PATH") or os.environ.get("PATH", "")
    found = shutil.which(name, path=search)
    return str(found) if found else None


def cpu_count() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def ensure_dir(path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_tree(src, dst) -> None:
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def static_lib_names(directory: Path) -> List[str]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix in (".a", ".lib"))


def lipo_create(runner: CommandRunner, output: Path, inputs: Sequence[Path]) -> None:
    inputs = [str(p) for p in inputs if Path(p).exists()]
    if not inputs:
        raise RuntimeError(f"lipo 输入为空: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    runner.run(["lipo", "-create", *inputs, "-output", str(output)])


def to_msys_path(path) -> str:
    """Windows 路径 -> MSYS2 POSIX 路径（D:\\x -> /d/x）。"""
    import re

    text = str(Path(path).resolve()).replace("\\", "/")
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


def write_version_file(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def git_head_sha(src_dir) -> str:
    """取源码目录的短 commit（7 位）；非 git 仓库/取不到时返回空串。"""
    import subprocess

    src = Path(src_dir)
    if not (src / ".git").exists():
        return ""
    try:
        result = subprocess.run(["git", "-C", str(src), "rev-parse", "--short=7", "HEAD"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def build_marker_name(library: str, version: str, platform: str, abi: str,
                      compiler: str, commit: str = "", when=None, profile: str = "") -> str:
    """版本文件名：version-<库>-<版本>-g<commit>-<时间>.<平台>[.<abi>][.<用途>].<编译器>.txt

    例：
      version-ffmpeg-6.1.6-g3c1fc5d-2026-10-19-10-32.windows.x64.capture.msvc.txt
      version-spdlog-v1.14.1-g27cb4c7-2026-10-19-20-36.windows.x64.msvc.txt
    """
    import re
    from datetime import datetime as _dt

    when = when or _dt.now()

    def safe(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z._-]+", "-", str(text)).strip("-") or "unknown"

    parts = ["version", safe(library), safe(version)]
    if commit:
        parts.append("g" + safe(commit)[:9])
    parts.append(when.strftime("%Y-%m-%d-%H-%M"))
    tail = [safe(platform)]
    if abi:
        tail.append(safe(abi))
    if profile:
        tail.append(safe(profile))
    tail.append(safe(compiler))
    return ".".join(["-".join(parts), *tail]) + ".txt"


def fix_pkgconfig_prefixes(install_dir: Path) -> None:
    """把 *.pc 里的 prefix/libdir 指向最终产物目录。

    这些文件在安装时被写成了构建暂存目录（甚至 MSYS 形式），
    交给 ffmpeg 的 pkg-config 使用时会失效。
    """
    import re

    pc_dir = Path(install_dir) / "lib" / "pkgconfig"
    if not pc_dir.is_dir():
        return
    prefix = str(install_dir).replace("\\", "/")
    normalized = {"exec_prefix": "${prefix}", "libdir": "${prefix}/lib", "includedir": "${prefix}/include"}
    for pc_file in pc_dir.glob("*.pc"):
        text = pc_file.read_text(encoding="utf-8", errors="ignore")
        updated = re.sub(r"^prefix=.*$", f"prefix={prefix}", text, flags=re.MULTILINE)
        for key, value in normalized.items():
            updated = re.sub(rf"^{key}=(?!\$\{{).*$", f"{key}={value}", updated, flags=re.MULTILINE)
        if updated != text:
            pc_file.write_text(updated, encoding="utf-8")
