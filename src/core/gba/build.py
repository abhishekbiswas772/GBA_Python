"""Build the GBA core: ``uv run python -m src.core.gba.build [--debug]``.

Runs ``cargo build`` on ``native/`` against the current Python and copies the
library next to the package as ``_gba.pyd`` / ``_gba.so``. Needs Rust; on
Windows also the MSVC build tools (Visual Studio Build Tools, C++ workload).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = HERE / "native"


def _cargo() -> str:
    found = shutil.which("cargo")
    if found:
        return found
    home = Path.home() / ".cargo" / "bin" / ("cargo.exe" if os.name == "nt" else "cargo")
    if home.is_file():
        return str(home)
    sys.exit("cargo not found: install Rust from https://rustup.rs and try again")


def build(release: bool = True) -> Path:
    env = dict(os.environ, PYO3_PYTHON=sys.executable)
    if os.name == "nt":
        # Git for Windows puts a GNU `link.exe` on PATH; keep it away from rustc
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if "Git\\usr\\bin" not in p)
    cmd = [_cargo(), "build", "--manifest-path", str(NATIVE / "Cargo.toml")]
    if release:
        cmd.append("--release")
    subprocess.run(cmd, check=True, env=env)
    profile = "release" if release else "debug"
    out_dir = NATIVE / "target" / profile
    if sys.platform == "win32":
        src, dst = out_dir / "_gba.dll", HERE / "_gba.pyd"
    elif sys.platform == "darwin":
        src, dst = out_dir / "lib_gba.dylib", HERE / "_gba.so"
    else:
        src, dst = out_dir / "lib_gba.so", HERE / "_gba.so"
    if dst.exists():
        # Windows locks a loaded extension but allows renaming it: move it aside
        old = dst.with_name(dst.stem + ".old" + dst.suffix)
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            dst.replace(old)
        except OSError:
            pass
    shutil.copyfile(src, dst)
    print(f"built {dst}")
    return dst


if __name__ == "__main__":
    build(release="--debug" not in sys.argv)
