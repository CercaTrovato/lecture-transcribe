"""Build a model-free runtime component from a portable Python distribution.

Use on each target OS/architecture, with that platform's installed dependencies.
This does not modify the Python installation or include user/model data.
"""
import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


def archive_runtime(python_home, site_packages, llama_dir, output, cuda=False):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        def add_tree(source, prefix, exclude=()):
            if not source.exists():
                return
            for file in source.rglob("*"):
                if not file.is_file() or any(part in exclude for part in file.relative_to(source).parts):
                    continue
                archive.write(file, str(Path(prefix) / file.relative_to(source)))
        if cuda:
            add_tree(site_packages / "nvidia", "site-packages/nvidia", ("__pycache__",))
            for file in llama_dir.glob("*"):
                if file.is_file() and (file.suffix in (".dll", ".dylib") or ".so" in file.name or file.name in ("llama-server", "llama-server.exe")):
                    archive.write(file, "llama/" + file.name)
        else:
            if sys.platform == "win32":
                for file in python_home.iterdir():
                    if file.is_file() and (file.suffix == ".dll" or file.name == "python.exe"):
                        archive.write(file, "python/" + file.name)
                add_tree(python_home / "DLLs", "python/DLLs")
                add_tree(python_home / "Lib", "python/Lib", ("site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo"))
                add_tree(site_packages, "python/Lib/site-packages", ("nvidia", "__pycache__", "pip", "setuptools", "wheel"))
            else:
                add_tree(python_home, "python", ("__pycache__", "test", "tests"))
                version = f"python{sys.version_info.major}.{sys.version_info.minor}"
                add_tree(site_packages, f"python/lib/{version}/site-packages", ("nvidia", "__pycache__", "pip", "setuptools", "wheel"))
            for file in llama_dir.glob("*"):
                if file.is_file() and (file.name.startswith(("ggml-cpu", "ggml-base", "llama", "libllama", "libggml", "libomp")) or file.name in ("ggml.dll",)):
                    if "cuda" not in file.name and "bench" not in file.name and "cli" not in file.name:
                        archive.write(file, "llama/" + file.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-home", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--llama-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--cuda", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    model_files = list(args.site_packages.rglob("*.gguf")) + list(args.site_packages.rglob("model.bin"))
    if model_files:
        raise SystemExit("Dependency tree contains model weights; refusing to package them")
    component_id = ("cuda-" if args.cuda else "cpu-") + args.platform
    output = args.output / (component_id + ".zip")
    archive_runtime(args.python_home.resolve(), args.site_packages.resolve(), args.llama_dir.resolve(), output, args.cuda)
    sha = hashlib.sha256()
    with output.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
    manifest_path = args.output / "components.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"schema": 1, "components": {}}
    entry = "llama/llama-server.exe" if args.cuda and args.platform.startswith("win32") else "llama/llama-server" if args.cuda else "python/python.exe" if args.platform.startswith("win32") else "python/bin/python3"
    manifest["components"][component_id] = {"url": output.name, "bytes": output.stat().st_size, "sha256": sha.hexdigest(), "entry": entry}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"component": component_id, "bytes": output.stat().st_size, "sha256": sha.hexdigest()}), flush=True)


if __name__ == "__main__":
    main()
