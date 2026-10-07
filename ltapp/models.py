"""Explicit model installation. Never download weights during inference/startup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .config import MODEL_DIR

CATALOG = {
    "asr-turbo": {"name": "Whisper Turbo", "bytes": 1617884929, "kind": "asr", "recommended": True,
                  "purpose": "标准实时转录与完整转录共用", "impact": "需要改用其他已安装的转录模型；没有转录模型时只能录音保存、导入和查看历史。",
                  "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "filename": "model.bin"},
    "asr-large": {"name": "Whisper large-v3", "bytes": 3087284237, "kind": "asr", "recommended": False,
                  "purpose": "高精度完整转录增强包", "impact": "高精度模式不可用；已安装 Turbo 时标准转录仍可用。",
                  "repo": "Systran/faster-whisper-large-v3", "filename": "model.bin"},
    "mt-live": {"name": "Hy-MT2 1.8B Q4", "bytes": 1133080448, "kind": "translation", "recommended": True,
                "purpose": "实时中文译文", "impact": "实时只显示原文；录音和转录不受影响，已有译文保留。",
                "repo": "tencent/Hy-MT2-1.8B-GGUF", "filename": "Hy-MT2-1.8B-Q4_K_M.gguf"},
    "mt-final": {"name": "Hy-MT2 7B Q4", "bytes": 4624648896, "kind": "translation", "recommended": False,
                 "purpose": "完整转录的最终整篇精译增强包", "impact": "不能生成最终精译稿；已有实时双语稿和原文仍保留。",
                 "repo": "tencent/Hy-MT2-7B-GGUF", "filename": "Hy-MT2-7B-Q4_K_M.gguf"},
}


class ModelError(ValueError):
    pass


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class ModelStore:
    def __init__(self, root: Path = MODEL_DIR, metadata_provider=None):
        self.root = root.resolve()
        self.managed = self.root / "managed"
        self.partial = self.root / "partial"
        self.registry = self.root / "references.json"
        self._lock = threading.RLock()
        self._users = {}
        self._tasks = {}
        self._cancels = {}
        self.metadata_provider = metadata_provider or self._remote_manifest
        self.on_unload = lambda model_id: None

    def spec(self, model_id):
        if model_id not in CATALOG:
            raise ModelError("未知模型")
        spec = {**CATALOG[model_id], "id": model_id, "backend": "llama.cpp" if CATALOG[model_id]["kind"] == "translation" else "ctranslate2"}
        if spec["kind"] == "asr" and sys.platform == "darwin":
            spec.update(repo="ggerganov/whisper.cpp", backend="whisper.cpp",
                        filename="ggml-large-v3-turbo.bin" if model_id == "asr-turbo" else "ggml-large-v3.bin")
        return spec

    def _references(self):
        return json.loads(self.registry.read_text(encoding="utf-8")) if self.registry.exists() else {}

    def path(self, model_id):
        spec = self.spec(model_id)
        with self._lock:
            reference = self._references().get(model_id)
            if reference:
                p = Path(reference)
                needed = p / "model.bin" if spec["kind"] == "asr" and spec["backend"] == "ctranslate2" else p
                return p if needed.is_file() else None
            folder = self.managed / model_id
            receipt = folder / "installed.json"
            if not receipt.exists():
                return None
            data = json.loads(receipt.read_text(encoding="utf-8"))
            if any(not (folder / f["name"]).is_file() or (folder / f["name"]).stat().st_size != f["size"] for f in data["files"]):
                return None
            result = folder if spec["kind"] == "asr" and spec["backend"] == "ctranslate2" else folder / spec["filename"]
            return result if result.is_dir() or result.is_file() else None

    def choose_asr(self, preferred="asr-turbo"):
        for model_id in (preferred, "asr-turbo", "asr-large"):
            if self.path(model_id):
                return model_id
        return None

    def acquire(self, model_id):
        with self._lock:
            if not self.path(model_id):
                raise ModelError(f"尚未安装 {self.spec(model_id)['name']}，请在模型管理中主动安装。")
            self._users[model_id] = self._users.get(model_id, 0) + 1

    def release(self, model_id):
        with self._lock:
            self._users[model_id] = max(0, self._users.get(model_id, 0) - 1)

    @contextmanager
    def using(self, model_id):
        self.acquire(model_id)
        try:
            yield self.path(model_id)
        finally:
            self.release(model_id)

    def list(self):
        with self._lock:
            refs = self._references()
            result = []
            for model_id in CATALOG:
                spec = self.spec(model_id)
                path = self.path(model_id)
                result.append({**spec, "installed": path is not None, "source": "external" if model_id in refs else "managed",
                               "path": str(path) if path else "", "in_use": self._users.get(model_id, 0),
                               "installed_bytes": sum(p.stat().st_size for p in (self.managed / model_id).rglob("*") if p.is_file()) if path and model_id not in refs else 0,
                               "download": dict(self._tasks.get(model_id, {}))})
            return result

    def reference(self, model_id, path):
        spec = self.spec(model_id)
        p = Path(path).expanduser().resolve()
        if p.is_relative_to(self.managed.resolve()):
            raise ModelError("这是模型管理器拥有的模型目录，请直接使用已安装模型，无需外部引用。")
        needed = p / "model.bin" if spec["kind"] == "asr" and spec["backend"] == "ctranslate2" else p
        if not needed.is_file():
            raise ModelError("指定路径不是有效的模型文件或模型目录")
        with self._lock:
            self._check_idle(model_id)
            self.on_unload(model_id)
            refs = self._references()
            refs[model_id] = str(p)
            atomic_json(self.registry, refs)

    def change_storage(self, path):
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise ModelError("请选择绝对目录路径")
        with self._lock:
            for model_id in CATALOG:
                self._check_idle(model_id)
            for model_id in CATALOG:
                self.on_unload(model_id)
            self.root = target.resolve()
            self.managed = self.root / "managed"
            self.partial = self.root / "partial"
            self.registry = self.root / "references.json"
            self._tasks.clear()

    def _check_idle(self, model_id):
        self.spec(model_id)
        if self._users.get(model_id, 0):
            raise ModelError("模型正在被录音、排队或处理任务使用，请等任务结束后再操作。")
        if self._tasks.get(model_id, {}).get("state") in ("downloading", "cancelling", "verifying"):
            raise ModelError("模型正在下载，请先取消并等下载停止。")

    def uninstall(self, model_id):
        with self._lock:
            self._check_idle(model_id)
            self.on_unload(model_id)
            refs = self._references()
            if model_id in refs:
                del refs[model_id]
                atomic_json(self.registry, refs)
                return {"action": "reference_removed", "freed_bytes": 0}
            folder = self.managed / model_id
            if folder.exists():
                if folder.is_symlink() or folder.resolve().parent != self.managed.resolve():
                    raise ModelError("模型目录指向外部路径，拒绝删除")
                size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
                shutil.rmtree(folder)
            else:
                size = 0
            self._tasks.pop(model_id, None)
            return {"action": "uninstalled", "freed_bytes": size}

    def clear_partial(self, model_id):
        with self._lock:
            self._check_idle(model_id)
            folder = self.partial / model_id
            if folder.exists():
                if folder.is_symlink() or folder.resolve().parent != self.partial.resolve():
                    raise ModelError("下载目录指向外部路径，拒绝删除")
                shutil.rmtree(folder)
            self._tasks.pop(model_id, None)

    def cancel(self, model_id):
        self.spec(model_id)
        with self._lock:
            event = self._cancels.get(model_id)
            if event and self._tasks[model_id]["state"] in ("downloading", "verifying"):
                event.set()
                self._tasks[model_id]["state"] = "cancelling"

    def download(self, model_id):
        with self._lock:
            self.spec(model_id)
            if self.path(model_id):
                raise ModelError("模型已安装")
            if model_id in self._references():
                raise ModelError("存在外部模型引用，请先移除引用，再下载到管理目录。")
            if self._tasks.get(model_id, {}).get("state") in ("downloading", "cancelling", "verifying"):
                raise ModelError("模型已在下载")
            cancel = threading.Event()
            self._cancels[model_id] = cancel
            self._tasks[model_id] = {"state": "downloading", "bytes": 0, "total": self.spec(model_id)["bytes"], "error": ""}
            threading.Thread(target=self._download, args=(model_id, cancel), daemon=True).start()
            return dict(self._tasks[model_id])

    def _remote_manifest(self, spec):
        url = f"https://huggingface.co/api/models/{spec['repo']}?blobs=true"
        with urllib.request.urlopen(url, timeout=20) as response:
            info = json.load(response)
        files = []
        allowed = {spec["filename"]}
        if spec["kind"] == "asr" and spec["backend"] == "ctranslate2":
            allowed |= {"config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt", "preprocessor_config.json"}
        for sibling in info["siblings"]:
            name = sibling["rfilename"]
            if name in allowed:
                lfs = sibling.get("lfs")
                files.append({"name": name, "size": sibling["size"], "digest": lfs["sha256"] if lfs else sibling["blobId"],
                              "algorithm": "sha256" if lfs else "git-sha1",
                              "url": f"https://huggingface.co/{spec['repo']}/resolve/{info['sha']}/{name}"})
        if not any(f["name"] == spec["filename"] for f in files):
            raise ModelError("官方模型清单缺少权重文件")
        return {"revision": info["sha"], "files": files}

    def _download(self, model_id, cancel):
        task = self._tasks[model_id]
        try:
            folder = self.partial / model_id
            folder.mkdir(parents=True, exist_ok=True)
            if folder.is_symlink() or folder.resolve().parent != self.partial.resolve():
                raise ModelError("下载目录不能指向外部路径")
            receipt = folder / "manifest.json"
            manifest = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else self.metadata_provider(self.spec(model_id))
            if not any(f["name"] == self.spec(model_id)["filename"] for f in manifest["files"]):
                raise ModelError("模型清单缺少所选模型的权重文件")
            atomic_json(receipt, manifest)
            task["total"] = sum(f["size"] for f in manifest["files"])
            completed = 0
            for file in manifest["files"]:
                if Path(file["name"]).name != file["name"] or file["name"] in (".", ".."):
                    raise ModelError("模型清单包含非法文件名")
                part = folder / (file["name"] + ".part")
                offset = part.stat().st_size if part.exists() else 0
                if offset > file["size"]:
                    part.unlink()
                    offset = 0
                if offset < file["size"]:
                    request = urllib.request.Request(file["url"], headers={"Range": f"bytes={offset}-"} if offset else {})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        if offset and response.status == 206:
                            content_range = response.headers.get("Content-Range", "")
                            if not content_range.startswith(f"bytes {offset}-"):
                                raise ModelError("服务器返回了不匹配的续传位置")
                        else:
                            offset = 0
                        with part.open("ab" if offset else "wb") as stream:
                            while True:
                                if cancel.is_set():
                                    raise InterruptedError
                                chunk = response.read(65536)
                                if not chunk:
                                    break
                                stream.write(chunk)
                                offset += len(chunk)
                                task["bytes"] = completed + offset
                                if offset > file["size"]:
                                    raise ModelError("下载超过声明的文件大小")
                task["state"] = "verifying" if not cancel.is_set() else "cancelling"
                digest = hashlib.sha256() if file["algorithm"] == "sha256" else hashlib.sha1()
                if file["algorithm"] == "git-sha1":
                    digest.update(f"blob {file['size']}\0".encode())
                with part.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        if cancel.is_set():
                            raise InterruptedError
                        digest.update(chunk)
                if part.stat().st_size != file["size"] or digest.hexdigest() != file["digest"]:
                    part.unlink(missing_ok=True)
                    raise ModelError("模型完整性校验失败，请重试下载")
                completed += file["size"]
                task.update(bytes=completed, state="downloading")
            with self._lock:
                if cancel.is_set():
                    raise InterruptedError
                target = self.managed / model_id
                target.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.resolve().parent != self.managed.resolve():
                    raise ModelError("安装目录不能指向外部路径")
                for file in manifest["files"]:
                    (folder / (file["name"] + ".part")).replace(target / file["name"])
                atomic_json(target / "installed.json", manifest)
                task.update(state="installed", bytes=completed)
        except InterruptedError:
            task.update(state="cancelled", error="下载已取消，重试将从已下载位置继续。")
        except Exception as exc:
            task.update(state="cancelled" if cancel.is_set() else "error", error="下载已取消，重试将继续同一版本。" if cancel.is_set() else str(exc))
