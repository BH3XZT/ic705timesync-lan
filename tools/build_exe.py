"""把 ic705timesync-lan 打包成单文件 Windows exe（可复现）。

产物
----
    dist/ic705timesync-lan.exe     Windows onefile 控制台程序，内置 icom-lan

为什么不用 pip
--------------
本脚本用纯标准库完成"安装依赖 + 打包"，不依赖 pip / 虚拟环境：

    1. 从 PyPI 下载固定版本的 wheel（SSL 校验失败时自动退回不校验，兼容代理）；
    2. 用 zipfile 解压到 .build-libs/pylibs（相当于本地安装）；
    3. 把 src 与 vendor 目录放进 PYTHONPATH，调用 python -m PyInstaller；
    4. 可选：给 onefile 解包目录打 ACL 补丁（受限令牌 / 沙箱环境需要）。

用法
----
    python tools\\build_exe.py                 # 默认联网下载 PyInstaller 后打包
    python tools\\build_exe.py --no-download   # 复用已有库（.build-libs/pylibs
                                               # 或工作区 .tools/pylibs）
    python tools\\build_exe.py --clean         # 清掉 build/dist 后重建
    python tools\\build_exe.py --patch-acl     # 额外修补 onefile 的临时目录 ACL

依赖目录约定（都可被参数覆盖）
------------------------------
    .build-libs/                构建缓存（已 gitignore，别提交）
    .build-libs/pylibs/         PyInstaller 及其依赖（wheel 解压）
    ../.tools/vendor/           icom-lan、serial（由使用者提供，不随仓库分发）
    ../.tools/pylibs/           本机已有的 PyInstaller（离线回退）

注意：icom-lan 是运行时可选依赖，仓库本身不含它；打 exe 时通过
``--paths <vendor>`` + ``--collect-submodules icom_lan`` 把它静态收进去。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
BUILD_LIBS = ROOT / ".build-libs"
DEFAULT_PYLIBS = BUILD_LIBS / "pylibs"
ENTRY_FILE = BUILD_LIBS / "entry.py"
APP_NAME = "ic705timesync-lan"

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

# 与 Python 3.10–3.12 / Windows x64 兼容的一组版本（2026-11 实测可用）。
PINNED_WHEELS = [
    ("pyinstaller", "6.22.3"),
    ("pyinstaller-hooks-contrib", "2026.9"),
    ("altgraph", "0.17.5"),
    ("packaging", "26.3"),
    ("setuptools", "84.2.1"),
    ("pefile", "2024.8.26"),
    ("pywin32-ctypes", "0.2.3"),
]

EXCLUDED_MODULES = [
    "tkinter",
    "matplotlib",
    "scipy",
    "PIL",
]

# PyInstaller 6.x onefile 解包目录的 DACL 格式串（UTF-16）。原串只授予当前
# 用户；在受限令牌 / 沙箱里还要放行 restricting SID（通常 S-1-1-0 Everyone），
# 否则解包第一步就 EACCES。两串等长，可原地替换。
ACL_FORMAT_OLD = "D:(A;;FA;;;%s)"
ACL_FORMAT_NEW = "D:(A;;FA;;;WD)"


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------


def _candidate_vendor_dirs() -> list[Path]:
    """返回可能存放 icom_lan / serial 的目录（按优先级）。"""
    env = os.environ.get("IC705TIMESYNC_VENDOR")
    dirs = [Path(env)] if env else []
    dirs += [
        ROOT.parent / ".tools" / "vendor",
        ROOT.parent.parent / ".tools" / "vendor",
    ]
    return [d for d in dirs if d.is_dir()]


def _candidate_pylibs() -> list[Path]:
    """返回可能已有 PyInstaller 的目录（离线回退用）。"""
    return [
        ROOT.parent / ".tools" / "pylibs",
        ROOT.parent.parent / ".tools" / "pylibs",
    ]


# ---------------------------------------------------------------------------
# 下载 / 解压 wheel
# ---------------------------------------------------------------------------


def _ssl_contexts() -> list[ssl.SSLContext]:
    strict = ssl.create_default_context()
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    return [strict, loose]


def _http_get(url: str, timeout: float = 120.0) -> bytes:
    last_exc: Exception | None = None
    for ctx in _ssl_contexts():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ic705timesync-lan-build/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 换下一个 SSL 上下文重试
            last_exc = exc
    raise RuntimeError(f"下载失败: {url}\n最后一次错误: {last_exc}") from last_exc


def _platform_wheel_tag() -> str:
    if sys.platform != "win32":
        raise SystemExit(f"本脚本只用于 Windows 打包（当前平台 {sys.platform}）")
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "win_amd64"
    if machine in ("arm64", "aarch64"):
        return "win_arm64"
    return "win32"


def _wheel_is_compatible(filename: str, want_tag: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]
    cur = f"cp{sys.version_info.major}{sys.version_info.minor}"
    ok_py = (
        "py3" in py_tag.split(".")
        or cur in py_tag.split(".")
        or "py2.py3" in py_tag
        or py_tag == "py2"
    )
    if not ok_py:
        return False
    if abi_tag not in ("none", "abi3"):
        return False
    return plat_tag in ("any", want_tag)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_wheel(name: str, version: str, cache_dir: Path, want_tag: str) -> Path:
    meta = json.loads(_http_get(PYPI_JSON.format(name=name, version=version)))
    candidates = [
        f
        for f in meta.get("urls", [])
        if f.get("packagetype") == "bdist_wheel"
        and _wheel_is_compatible(f["filename"], want_tag)
    ]
    if not candidates:
        raise RuntimeError(
            f"PyPI 上 {name}=={version} 没有适用于 {want_tag} / "
            f"Python {sys.version_info.major}.{sys.version_info.minor} 的 wheel"
        )
    candidates.sort(key=lambda f: 0 if f["filename"].endswith("-none-any.whl") else 1)
    info = candidates[0]
    dest = cache_dir / info["filename"]
    expected = info.get("digests", {}).get("sha256", "")
    if dest.exists() and (not expected or _sha256(dest) == expected):
        print(f"  [cached] {info['filename']}")
        return dest
    print(f"  [download] {info['filename']} ({info['size'] / 1024:.0f} KiB)")
    data = _http_get(info["url"])
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"{info['filename']} SHA256 校验失败")
    dest.write_bytes(data)
    return dest


def install_wheels(libs_dir: Path, *, download: bool) -> dict[str, str]:
    """下载并解压 PyInstaller 依赖，返回 {包名: 版本}。"""
    libs_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = BUILD_LIBS / "wheels"
    cache_dir.mkdir(parents=True, exist_ok=True)
    want_tag = _platform_wheel_tag()

    installed: dict[str, str] = {}
    for name, version in PINNED_WHEELS:
        if not download:
            continue
        print(f"准备 {name}=={version}")
        wheel = download_wheel(name, version, cache_dir, want_tag)
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(libs_dir)
        installed[name] = version

    if download:
        manifest = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": want_tag,
            "wheels": installed,
        }
        (libs_dir / "_build_libs_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    return installed


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------


def write_entry() -> Path:
    """生成 PyInstaller 入口脚本（包内相对导入不能直接当脚本跑）。"""
    BUILD_LIBS.mkdir(parents=True, exist_ok=True)
    ENTRY_FILE.write_text(
        "from ic705timesync.cli import main\n\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    return ENTRY_FILE


def run_pyinstaller(
    *,
    python: str,
    pylibs: Path,
    vendor_dirs: list[Path],
    dist_dir: Path,
    work_dir: Path,
    spec_dir: Path,
) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    path_parts = [str(SRC_DIR), str(pylibs)] + [str(d) for d in vendor_dirs]
    if env.get("PYTHONPATH"):
        path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # PyInstaller 默认缓存写到 %LOCALAPPDATA%，沙箱/受限账户下可能被拒
    env["PYINSTALLER_CONFIG_DIR"] = str(work_dir / "pyinstaller-cache")

    cmd = [
        python,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--name",
        APP_NAME,
        "--paths",
        str(SRC_DIR),
        "--paths",
        str(pylibs),
    ]
    for d in vendor_dirs:
        cmd += ["--paths", str(d)]
    cmd += [
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--noconfirm",
        "--clean",
        "--collect-submodules",
        "icom_lan",
        "--hidden-import",
        "platformdirs",
        "--hidden-import",
        "serial",
        "--hidden-import",
        "serial.tools",
        "--hidden-import",
        "serial.tools.list_ports",
    ]
    for mod in EXCLUDED_MODULES:
        cmd.append(f"--exclude-module={mod}")
    # numpy 是 icom-lan 的可选 DSP 依赖；装了就打进去，没装就跳过（读取 CI-V 不需要）
    try:
        import importlib.util

        if importlib.util.find_spec("numpy") is not None:
            cmd += ["--collect-submodules", "numpy"]
    except Exception:  # noqa: BLE001 - 探测失败不影响主流程
        pass
    cmd.append(str(ENTRY_FILE))

    print("\n执行打包命令:")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")

    exe = dist_dir / (APP_NAME + ".exe")
    if not exe.exists():
        raise SystemExit(f"打包命令返回成功，但找不到产物: {exe}")
    return exe


def patch_onefile_temp_acl(exe: Path) -> str:
    """把 onefile 解包目录的 DACL 从"仅当前用户"改成"Everyone 全权"。

    仅用于在受限令牌 / 沙箱环境里启动 exe（PyInstaller 6.x 的 _MEI 目录默认
    只给当前用户 FA，沙箱进程的 restricting SID 会被拒）。这是**等长替换**
    bootloader 里的 SDDL 格式串。默认不启用：正常桌面环境不需要，且会让
    同机其他用户可读临时解包目录。
    """
    data = bytearray(exe.read_bytes())
    old = ACL_FORMAT_OLD.encode("utf-16-le") + b"\x00\x00"
    new = ACL_FORMAT_NEW.encode("utf-16-le") + b"\x00\x00"
    positions: list[int] = []
    start = 0
    while True:
        i = data.find(old, start)
        if i < 0:
            break
        positions.append(i)
        start = i + 1
    if len(positions) != 1:
        raise SystemExit(
            f"ACL 补丁失败：在 {exe.name} 中匹配到 {len(positions)} 处 "
            f"{ACL_FORMAT_OLD!r}（预期 1 处）。PyInstaller 版本可能变了。"
        )
    offset = positions[0]
    data[offset : offset + len(old)] = new
    exe.write_bytes(bytes(data))
    return f"已打 ACL 补丁 @ 0x{offset:X}: {ACL_FORMAT_OLD} -> {ACL_FORMAT_NEW}"


def verify(exe: Path) -> bool:
    """跑 --version / --help，确认 exe 可独立运行。"""
    ok = True
    for args in (["--version"], ["--help"]):
        print(f"\n[自检] {exe.name} {' '.join(args)}")
        proc = subprocess.run(
            [str(exe), *args],
            capture_output=True,
            timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        print(out.rstrip())
        if proc.returncode != 0:
            print(f"[自检] 失败：退出码 {proc.returncode}")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="下载 PyInstaller + 收集 icom_lan，生成单文件 ic705timesync-lan.exe"
    )
    p.add_argument("--python", default=sys.executable, help="用于打包的 Python 解释器")
    p.add_argument(
        "--pylibs",
        default=str(DEFAULT_PYLIBS),
        help=f"PyInstaller 依赖目录（默认 {DEFAULT_PYLIBS}）",
    )
    p.add_argument("--vendor", action="append", default=None,
                   help="额外 icom_lan/serial 目录（可多次；默认自动探测 .tools/vendor）")
    p.add_argument("--dist-dir", default=str(ROOT / "dist"))
    p.add_argument("--work-dir", default=str(ROOT / "build"))
    p.add_argument("--spec-dir", default=str(ROOT / "build"))
    p.add_argument("--no-download", action="store_true",
                   help="不联网下载；复用 .build-libs/pylibs 或工作区 .tools/pylibs")
    p.add_argument("--clean", action="store_true",
                   help="构建前删除 dist/ 与 build/")
    p.add_argument("--patch-acl", action="store_true",
                   help="给 onefile 打 ACL 补丁（受限令牌/沙箱环境启动需要）")
    p.add_argument("--no-verify", action="store_true", help="跳过构建后的自检")
    return p.parse_args(argv)


def _icom_lan_dir_via_python(python: str) -> Path | None:
    """在目标解释器的环境里 import icom_lan，返回其所在目录的父目录。

    这样使用者只要 ``pip install icom-lan``，不必手工指定 --vendor。
    """
    probe = subprocess.run(
        [
            python,
            "-c",
            "import icom_lan, os; print(os.path.dirname(os.path.dirname(icom_lan.__file__)))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if probe.returncode != 0:
        return None
    path = probe.stdout.strip()
    return Path(path) if path and (Path(path) / "icom_lan").is_dir() else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not SRC_DIR.is_dir():
        raise SystemExit(f"缺少源码目录: {SRC_DIR}")

    vendor_dirs = [Path(d) for d in args.vendor] if args.vendor else _candidate_vendor_dirs()
    pylibs = Path(args.pylibs)
    print("=" * 72)
    print(f"项目根目录 : {ROOT}")
    print(f"Python     : {args.python}")
    print(f"依赖目录   : {pylibs}")

    probe = subprocess.run(
        [args.python, "-c", "import sys; print(sys.version)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if probe.returncode != 0:
        raise SystemExit(f"无法执行 Python: {args.python}\n{probe.stderr}")
    print("解释器版本 :", probe.stdout.strip(), "\n")

    if not any((d / "icom_lan").is_dir() for d in vendor_dirs):
        installed_dir = _icom_lan_dir_via_python(args.python)
        if installed_dir is not None:
            print(f"在解释器环境里找到 icom_lan: {installed_dir}")
            vendor_dirs.append(installed_dir)
    if not any((d / "icom_lan").is_dir() for d in vendor_dirs):
        raise SystemExit(
            "找不到 icom_lan。请先 `pip install icom-lan`，或用 --vendor 指定其所在目录，"
            "或设置环境变量 IC705TIMESYNC_VENDOR。"
        )
    print(f"icom_lan   : {next(d for d in vendor_dirs if (d / 'icom_lan').is_dir())}")
    print("=" * 72)

    if args.clean:
        for d in (ROOT / "dist", ROOT / "build"):
            if d.exists():
                print(f"删除旧目录: {d}")
                shutil.rmtree(d)

    if not args.no_download:
        installed = install_wheels(pylibs, download=True)
        print("\n已就绪的依赖:")
        for name, version in installed.items():
            print(f"  {name}=={version}")
    else:
        if not (pylibs / "PyInstaller").is_dir():
            for fallback in _candidate_pylibs():
                if (fallback / "PyInstaller").is_dir():
                    print(f"--no-download：改用已有 PyInstaller 目录 {fallback}")
                    pylibs = fallback
                    break
            else:
                raise SystemExit(
                    f"--no-download 但 {pylibs} 里没有 PyInstaller；"
                    "请先联网跑一次，或用 --pylibs 指定。"
                )

    # 确认 PyInstaller 可用（PYTHONPATH 生效）
    check_env = os.environ.copy()
    check_env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR), str(pylibs)] + [str(d) for d in vendor_dirs]
    )
    check = subprocess.run(
        [args.python, "-m", "PyInstaller", "--version"],
        capture_output=True,
        env=check_env,
        text=True,
        encoding="utf-8",
    )
    if check.returncode != 0:
        raise SystemExit(f"无法运行 PyInstaller:\n{check.stderr}")
    print("PyInstaller  :", check.stdout.strip())

    entry = write_entry()
    print("入口脚本     :", entry)

    exe = run_pyinstaller(
        python=args.python,
        pylibs=pylibs,
        vendor_dirs=vendor_dirs,
        dist_dir=Path(args.dist_dir),
        work_dir=Path(args.work_dir),
        spec_dir=Path(args.spec_dir),
    )

    size_mb = exe.stat().st_size / (1024 * 1024)
    print("\n" + "=" * 72)
    print(f"构建完成: {exe}")
    print(f"文件大小: {exe.stat().st_size:,} 字节 ({size_mb:.2f} MiB)")
    print(f"SHA256  : {_sha256(exe)}")
    if args.patch_acl:
        print(patch_onefile_temp_acl(exe))
    print("=" * 72)

    if not args.no_verify:
        if not verify(exe):
            return 1
        print("\n自检通过：exe 可以独立运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
