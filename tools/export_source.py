"""Explicit source-only export. Never traverse runtime/user data directories."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
files = []
for name in ("app.py", "transcribe.py", "lt.py", "record.py", "srt2txt.py", "README.md", "LICENSE", "pyproject.toml", ".python-version", ".gitignore"):
    path = root / name
    if path.exists():
        files.append(path)
allowed = {".py", ".js", ".html", ".css", ".ico", ".md", ".json", ".yml", ".yaml", ".png"}
for directory in ("ltapp", "web", "tests", "tools", "docs", ".github", "desktop"):
    for path in (root / directory).rglob("*"):
        if not path.is_file() or path.suffix not in allowed or any(part in ("node_modules", "__pycache__", "release", "target") for part in path.relative_to(root).parts):
            continue
        if path.relative_to(root).as_posix() == "docs/HANDOFF.md":
            continue
        files.append(path)
args.output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(set(files)):
        archive.write(file, "lecture-transcribe/" + file.relative_to(root).as_posix())
print(json.dumps({"files": len(set(files)), "bytes": args.output.stat().st_size, "archive": str(args.output)}))
