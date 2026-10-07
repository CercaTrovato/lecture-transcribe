"""正在运行的外部进程（ffmpeg）登记表：让导入可以被取消，也让服务知道"还有活在干"。

ffmpeg 调用散落在 transcribe.py 和 library.py 里，这里用线程局部的 key 把它们挂到某条记录名下：
`import_audio` 在自己的线程里 `bind(rid)`，其间起的 ffmpeg 都记在 rid 名下，`cancel(rid)` 直接杀掉。
没有 bind 的调用（录音收尾、音频压缩）行为与 subprocess.run 完全一致。
"""

from __future__ import annotations

import contextlib
import subprocess
import threading

_local = threading.local()
_lock = threading.Lock()
_procs: dict[str, list[subprocess.Popen]] = {}
_cancelled: set[str] = set()


class Cancelled(Exception):
    """外部进程被 cancel() 杀掉。"""


@contextlib.contextmanager
def bind(key: str):
    prev = getattr(_local, "key", None)
    _local.key = key
    with _lock:
        _procs[key] = []
        _cancelled.discard(key)
    try:
        yield
    finally:
        _local.key = prev
        with _lock:
            _procs.pop(key, None)
            _cancelled.discard(key)


def active() -> list[str]:
    with _lock:
        return list(_procs)


def running(key: str) -> int:
    """key 名下正在跑的进程数（测试 / 诊断用）。"""
    with _lock:
        return len(_procs.get(key, []))


def cancel(key: str) -> bool:
    """杀掉这个 key 名下正在跑的进程；key 当前没有活动进程返回 False。"""
    with _lock:
        procs = _procs.get(key)
        if procs is None:
            return False
        _cancelled.add(key)
        victims = list(procs)
    for p in victims:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    return True


def run(cmd, *, check: bool = False, capture_output: bool = False, text: bool = False,
        encoding: str | None = None, errors: str | None = None) -> subprocess.CompletedProcess:
    """subprocess.run 的替身：把进程登记到当前线程的 key 上，被 cancel() 杀掉时抛 Cancelled。"""
    key = getattr(_local, "key", None)
    if key is not None:
        with _lock:
            if key in _cancelled:   # 上一段刚被取消：别再起新进程做无用功
                raise Cancelled()
    pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture_output else {}
    p = subprocess.Popen(cmd, text=text, encoding=encoding, errors=errors,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **pipes)
    if key is not None:
        with _lock:
            _procs.setdefault(key, []).append(p)
    try:
        out, err = p.communicate()
    finally:
        if key is not None:
            with _lock:
                if p in _procs.get(key, []):
                    _procs[key].remove(p)
    if key is not None:
        with _lock:
            if key in _cancelled:
                raise Cancelled()
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
