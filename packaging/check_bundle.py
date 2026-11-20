"""Check a `flet build` output folder: the app is there and the PS1 core (_ps1.pyd/.so) got bundled.

    python packaging/check_bundle.py <build output dir>

Looks inside zip archives too (flet may pack the app's Python files into one).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def find(root: Path, needle: str) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if needle in p.name:
            hits.append(str(p.relative_to(root)))
        elif p.suffix.lower() in (".zip", ".whl"):
            try:
                with zipfile.ZipFile(p) as z:
                    hits += [f"{p.relative_to(root)}!{n}" for n in z.namelist() if needle in n.rsplit("/", 1)[-1]]
            except zipfile.BadZipFile:
                pass
    return hits


def main() -> int:
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"no build output at {root}")
        return 1
    core = [h for h in find(root, "_ps1.") if h.endswith((".pyd", ".so")) and ".old." not in h]
    main_py = find(root, "main.py")
    print("main.py:", main_py[:3] or "MISSING")
    print("PS1 core:", core or "MISSING")
    if not main_py:
        return 1
    if not core:
        print("::error::the PS1 core (_ps1.pyd / _ps1.so) is not in the bundle")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
