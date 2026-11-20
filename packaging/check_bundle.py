"""Check a `flet build` output folder: the app is there and the native cores (_ps1 / _gba .pyd/.so) got bundled.

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
    main_py = find(root, "main.py")
    print("main.py:", main_py[:3] or "MISSING")
    ok = bool(main_py)
    for name, label in (("_ps1", "PS1"), ("_gba", "GBA")):
        core = [h for h in find(root, name + ".") if h.endswith((".pyd", ".so")) and ".old." not in h]
        print(f"{label} core:", core or "MISSING")
        if not core:
            print(f"::error::the {label} core ({name}.pyd / {name}.so) is not in the bundle")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
