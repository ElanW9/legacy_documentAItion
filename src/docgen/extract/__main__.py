"""Debug helper: `python -m docgen.extract <file-or-dir> [...]` prints FACTS JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from docgen.extract import extract_package, find_all_packages, known_package_names
from docgen.extract.package import pair_package_files


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m docgen.extract")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--root", type=Path, default=Path("."), help="repo root for relative paths")
    ap.add_argument("--plsql-root", type=Path, default=Path("plsql"))
    args = ap.parse_args()

    files: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            files.extend(f.spec or f.body for f in find_all_packages(p))
        else:
            files.append(p)
    known = known_package_names(args.plsql_root)
    out = [
        extract_package(pf, root=args.root, known_packages=known).facts.model_dump(mode="json")
        for pf in pair_package_files(files)
    ]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
