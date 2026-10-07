import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def user_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LectureTranscribe"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LectureTranscribe"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "lecture-transcribe"


DATA_DIR = Path(os.environ.get("LT_DATA_DIR", user_data_dir())).expanduser().resolve()
LIB_DIR = DATA_DIR / "library"
MODEL_DIR = Path(os.environ.get("LT_MODEL_DIR", DATA_DIR / "models")).expanduser().resolve()
SETTINGS_PATH = DATA_DIR / "settings.json"
if "LT_MODEL_DIR" not in os.environ and SETTINGS_PATH.exists():
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("model_dir")
        if isinstance(saved, str) and Path(saved).is_absolute():
            MODEL_DIR = Path(saved).expanduser().resolve()
    except (OSError, ValueError):
        pass
WEB_DIR = ROOT / "web"
HOST = "127.0.0.1"
PORT = int(os.environ.get("LT_PORT", "8765"))
SR = 16000                      # 全链路统一 16 kHz 单声道
COURSES_PATH = Path(os.environ.get("LT_COURSES", DATA_DIR / "courses.json"))
