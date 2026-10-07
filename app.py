"""启动 GUI：uv run app.py  →  http://127.0.0.1:8765
模型在后台加载（约 5–10 秒），页面立即可开。"""

import sys
import threading
import webbrowser

import uvicorn

from ltapp.config import HOST, PORT

if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    url = f"http://{HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"lecture-transcribe GUI → {url}")
    uvicorn.run("ltapp.server:app", host=HOST, port=PORT, log_level="warning")
