"""
lt.py — 给 agent / 命令行用的接口。GUI (app.py) 开着就走它的 API（复用已加载的模型），没开就本进程加载模型直接干。

    uv run lt.py list [-n 10] [--json]                      # 历史录音（最新在前）
    uv run lt.py latest [--json]                             # 最新一条
    uv run lt.py transcribe <id|latest> [--hotwords "..."] [--json]   # 转录（已转录过则直接返回产物），阻塞到完成
    uv run lt.py import <音频路径> [--name X] [--hotwords "..."] [--json]  # 导入本机文件到库并转录
    uv run lt.py show <id|latest> [--md|--srt]               # 打印转录文本（默认 Notta 兼容 txt）
    uv run lt.py path <id|latest> [--md|--srt]               # 只打印产物路径
    uv run lt.py status                                      # GUI 是否在跑、模型是否就绪、录音状态
    uv run lt.py export <id|latest> --module M04 [--overwrite]   # 复制 txt 为 <课程>/transcripts/M04-transcript.txt（课程取自录音的 course 字段）
    uv run lt.py courses                                     # 打印 courses.json 里的课程
    uv run lt.py translate <id|latest> [--json]              # （重新）做最终版中文翻译（需 GUI 运行），阻塞到完成；产物 files.zh_md

典型 agent 流程：用户说"录完了" → `lt.py list --json` 看最新几条（status=done 的已可直接用）→ 需要时
`lt.py transcribe latest --json` → 取 `files.txt` 复制/改名到 vault 的 transcripts/M0N-transcript.txt。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ltapp.config import HOST, PORT  # noqa: E402

BASE = os.environ.get("LT_URL", f"http://{HOST}:{PORT}")


# ---------------------------------------------------------------- 与 GUI 服务通信
def _http(method: str, path: str, body=None, timeout=5):
    import urllib.error
    import urllib.parse
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    path = urllib.parse.quote(path, safe="/?=&")  # id 里可能有中文
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:  # noqa: BLE001
            detail = e.reason
        raise RuntimeError(f"{e.code} {detail}")


def server_up() -> bool:
    try:
        _http("GET", "/api/status", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 本地直连（GUI 没开时）
def _local_lib():
    from ltapp.library import Library
    return Library()


def _local_transcribe(rid: str, hotwords: str | None, lang: str | None = None) -> dict:
    import transcribe
    lib = _local_lib()
    meta = lib.get(rid)
    if meta is None:
        sys.exit(f"找不到录音 {rid}")
    hw = hotwords if hotwords is not None else meta.get("hotwords") or None
    if lang is not None:
        meta = lib.update(rid, lang=lang)
    lib.update(rid, status="transcribing", hotwords=hw or "")
    print(f"GUI 未运行，本进程加载模型...", file=sys.stderr)
    from ltapp.engine import ModelHolder
    holder = ModelHolder()
    if not holder.ensure_main():
        sys.exit(holder.error)
    model = holder.model
    r = transcribe.transcribe_file(
        lib.audio(rid), model, out_dir=lib.dir(rid), stem="transcript", title=meta["name"],
        lang=meta.get("lang", "en"), hotwords=hw, normalize=False, log=lambda s: print(s, file=sys.stderr),
    )
    lib.save_segments(rid, r["segments"])
    lib.update(rid, status="done", duration=round(r["duration"], 2), error="", dropped=len(r["dropped"]),
               transcribed=time.strftime("%Y-%m-%dT%H:%M:%S"), elapsed=round(r["elapsed"], 1))
    return _get(rid)


# ---------------------------------------------------------------- 统一读接口（两种模式都用库文件）
def _list() -> list[dict]:
    return _local_lib().list()


def _get(rid: str) -> dict:
    lib = _local_lib()
    meta = lib.get(rid)
    if meta is None:
        sys.exit(f"找不到录音 {rid}")
    return {**meta, "files": lib.transcript_paths(rid), "audio": str(lib.audio(rid)), "dir": str(lib.dir(rid))}


def _resolve(ref: str) -> str:
    if ref == "latest":
        items = _list()
        if not items:
            sys.exit("库里没有录音")
        return items[0]["id"]
    return ref


def _wait_job(jid: str) -> dict:
    last = ""
    while True:
        j = _http("GET", f"/api/jobs/{jid}")
        msg = f"{j.get('phase', '')}  {j['log'][-1] if j['log'] else ''}"
        if msg != last:
            eta = f"  剩余≈{j['eta']}s" if j.get("eta") else ""
            print(f"  [{j['status']}] {int(j['progress'] * 100):3d}%{eta}  {msg}", file=sys.stderr)
            last = msg
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(2)


# ---------------------------------------------------------------- 命令
def cmd_list(a):
    items = _list()[: a.n]
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for m in items:
        print(f"{m['id']:<48} {m['status']:<12} {m['duration'] / 60:6.1f} min  {m.get('course', ''):<8} {m['name']}")


def cmd_latest(a):
    d = _get(_resolve("latest"))
    print(json.dumps(d, ensure_ascii=False, indent=2) if a.json else f"{d['id']}  {d['status']}  {d['files'].get('txt', '')}")


def cmd_transcribe(a):
    rid = _resolve(a.ref)
    d = _get(rid)
    if d["status"] == "done" and a.hotwords is None and a.lang is None and not a.force:
        print("已有转录，直接返回（--force 重转）", file=sys.stderr)
    else:
        if server_up():
            if d["status"] == "recording":
                sys.exit("该录音仍在进行中，先在 GUI 里点「结束并转录」")
            job = _http("POST", f"/api/recordings/{rid}/transcribe", {"hotwords": a.hotwords, "lang": a.lang})
            j = _wait_job(job["id"])
            if j["status"] in ("error", "cancelled"):
                sys.exit(f"转录失败: {j['error'] or j['status']}")
        else:
            _local_transcribe(rid, a.hotwords, a.lang)
        d = _get(rid)
    _emit(d, a)


def cmd_import(a):
    src = Path(a.path).resolve()
    if not src.is_file():
        sys.exit(f"找不到文件 {src}")
    if server_up():
        r = _http("POST", "/api/import-path", {"path": str(src), "name": a.name or src.stem, "hotwords": a.hotwords or "", "lang": a.lang}, timeout=600)
        j = _wait_job(r["job"]["id"])
        if j["status"] in ("error", "cancelled"):
            sys.exit(f"转录失败: {j['error'] or j['status']}")
        d = _get(r["recording"]["id"])
    else:
        lib = _local_lib()
        meta = lib.import_audio(src, a.name or src.stem, hotwords=a.hotwords or "", lang=a.lang)
        d = _local_transcribe(meta["id"], a.hotwords)
    _emit(d, a)


def cmd_show(a):
    d = _get(_resolve(a.ref))
    kind = "md" if a.md else "srt" if a.srt else "txt"
    p = d["files"].get(kind)
    if not p:
        sys.exit(f"{d['id']} 还没有转录产物（status={d['status']}），先跑 transcribe")
    sys.stdout.write(Path(p).read_text(encoding="utf-8"))


def cmd_path(a):
    d = _get(_resolve(a.ref))
    kind = "md" if a.md else "srt" if a.srt else "txt"
    p = d["files"].get(kind)
    if not p:
        sys.exit(f"{d['id']} 还没有转录产物（status={d['status']}）")
    print(p)


def cmd_status(a):
    if server_up():
        s = _http("GET", "/api/status")
        print(json.dumps({"gui": True, **s}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"gui": False, "hint": "GUI 未运行；transcribe/import 会在本进程加载模型"}, ensure_ascii=False, indent=2))


def cmd_export(a):
    from ltapp.library import Library
    lib = Library()
    rid = _resolve(a.ref)
    try:
        dst = lib.export_to_vault(rid, a.module, a.overwrite)
    except FileExistsError as e:
        sys.exit(f"{e}（加 --overwrite 覆盖）")
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    print(json.dumps({"id": rid, "exported": str(dst)}, ensure_ascii=False) if a.json else str(dst))


def cmd_translate(a):
    rid = _resolve(a.ref)
    if not server_up():
        sys.exit("最终版翻译需要 GUI 服务在运行（start_gui_bg.bat）")
    job = _http("POST", f"/api/recordings/{rid}/translate")
    j = _wait_job(job["id"])
    if j["status"] in ("error", "cancelled"):
        sys.exit(f"翻译失败: {j['error'] or j['status']}")
    _emit(_get(rid), a)


def cmd_courses(a):
    from ltapp.library import load_courses
    cs = load_courses()
    if a.json:
        print(json.dumps(cs, ensure_ascii=False, indent=2))
    elif not cs:
        print("courses.json 不存在或为空")
    else:
        for c in cs:
            print(f"{c['code']:<8} {c['name']:<32} next={c['next_module'] or '-':<5} hotwords={len(c['hotwords'].split(','))}  {c['transcripts_dir']}")


def _emit(d: dict, a):
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(f"{d['id']}  {d['status']}  {d['duration'] / 60:.1f} min")
        for k, p in d["files"].items():
            print(f"  {k}: {p}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("-n", type=int, default=10); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_list)
    s = sub.add_parser("latest"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_latest)
    s = sub.add_parser("transcribe"); s.add_argument("ref"); s.add_argument("--hotwords"); s.add_argument("--force", action="store_true"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_transcribe)
    s.add_argument("--lang", choices=["auto", "zh", "en"])
    s = sub.add_parser("import"); s.add_argument("path"); s.add_argument("--name"); s.add_argument("--hotwords"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_import)
    s.add_argument("--lang", choices=["auto", "zh", "en"], default="auto")
    for name, fn in (("show", cmd_show), ("path", cmd_path)):
        s = sub.add_parser(name); s.add_argument("ref"); s.add_argument("--md", action="store_true"); s.add_argument("--srt", action="store_true"); s.set_defaults(f=fn)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("export"); s.add_argument("ref"); s.add_argument("--module", required=True); s.add_argument("--overwrite", action="store_true"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_export)
    s = sub.add_parser("courses"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_courses)
    s = sub.add_parser("translate"); s.add_argument("ref"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_translate)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    main()
