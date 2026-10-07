# 开发者指南 / Developer guide

[简体中文](#简体中文) · [English](#english) · [README](../README.md)

## 简体中文

普通用户安装 Windows 程序不需要做下面这些步骤。[安装说明在这里](../README.md#在-windows-上安装)。

### 从源码运行

先准备 Python 3.12、Git 和 [uv](https://docs.astral.sh/uv/)，然后执行：

```powershell
git clone https://github.com/CercaTrovato/lecture-transcribe.git
cd lecture-transcribe
uv sync
uv run app.py
```

需要 NVIDIA 推理依赖时使用 `uv sync --extra cuda`；macOS Metal 绑定使用 `uv sync --extra metal`。Linux 还需系统 PortAudio 库，例如 Ubuntu/Debian 的 `sudo apt-get install libportaudio2`。源码检查在 Windows、macOS、Ubuntu 上通过，不代表所有硬件和原生安装器都已验收。

`uv run app.py` 启动本机服务并打开浏览器。源码模式的翻译还需要 llama.cpp 的 `llama-server`：通过 `LT_LLAMA_SERVER` 指向其可执行文件；没有它时，录音和转录仍可使用。模型仍在界面中由用户主动安装或引用。

### 配置与 API

| 环境变量 | 作用 |
| --- | --- |
| `LT_DATA_DIR` | 录音、文字和配置目录 |
| `LT_MODEL_DIR` | 模型目录；界面中也可选择 |
| `LT_DEVICE` | `auto`、`cpu`、`cuda` 或 `metal` |
| `LT_LLAMA_SERVER` | 翻译运行程序的路径 |
| `LT_PORT` / `LT_MT_PORT` | 本机服务 / 翻译服务端口 |

源码默认数据位置：Windows `%LOCALAPPDATA%\LectureTranscribe`，macOS `~/Library/Application Support/LectureTranscribe`，Linux `$XDG_DATA_HOME/lecture-transcribe`（默认 `~/.local/share/lecture-transcribe`）。桌面版位置见 README。

模型相关接口：`GET /api/models`，`POST /api/models/{id}/download`、`cancel`、`reference`，`DELETE /api/models/{id}`，`DELETE /api/models/{id}/partial`，`POST /api/models/storage`。`GET /api/status` 查看就绪、录音和任务状态。接口只接受本机 Host，拒绝其他网页触发写操作。桌面版会选择可用端口，不要固定假设为 8765。

### 检查与打包

```powershell
uv sync --extra test
uv run python -m unittest discover -s tests -v
cd desktop
npm ci
npx install-electron
npm test
```

`tools/build_runtime.py` 生成不含四个可选模型的运行组件。`tools/configure_release.py` 配置真实下载地址，再用 Electron Builder 构建。详细流程见 [PACKAGING.md](PACKAGING.md)，验证范围见 [STATUS.md](STATUS.md)。

## English

Windows users installing the released app do not need these steps. See [the installation guide](../README.en.md#install-on-windows).

### Run from source

Install Python 3.12, Git, and [uv](https://docs.astral.sh/uv/), then run:

```powershell
git clone https://github.com/CercaTrovato/lecture-transcribe.git
cd lecture-transcribe
uv sync
uv run app.py
```

Use `uv sync --extra cuda` for NVIDIA inference dependencies, or `uv sync --extra metal` for the macOS Metal binding. Linux also needs PortAudio, such as `sudo apt-get install libportaudio2` on Ubuntu/Debian. Source checks pass on Windows, macOS, and Ubuntu; this does not establish native installer or hardware support on every platform.

`uv run app.py` starts the local service and opens a browser. For translation in source mode, supply a llama.cpp `llama-server` executable through `LT_LLAMA_SERVER`. Recording and transcription work without it. Users still explicitly install or reference models in the interface.

### Configuration and APIs

| Environment variable | Purpose |
| --- | --- |
| `LT_DATA_DIR` | Recordings, text, and configuration |
| `LT_MODEL_DIR` | Model folder; also selectable in the interface |
| `LT_DEVICE` | `auto`, `cpu`, `cuda`, or `metal` |
| `LT_LLAMA_SERVER` | Path to the translation runtime executable |
| `LT_PORT` / `LT_MT_PORT` | Local app / translation service port |

Source-mode data defaults: `%LOCALAPPDATA%\LectureTranscribe` on Windows, `~/Library/Application Support/LectureTranscribe` on macOS, and `$XDG_DATA_HOME/lecture-transcribe` on Linux, falling back to `~/.local/share/lecture-transcribe`. Desktop app paths are in the README.

Model APIs: `GET /api/models`; `POST /api/models/{id}/download`, `cancel`, and `reference`; `DELETE /api/models/{id}`; `DELETE /api/models/{id}/partial`; and `POST /api/models/storage`. `GET /api/status` reports readiness, recording, and tasks. The service accepts local Host values and rejects write requests triggered by other websites. The desktop app chooses an available port; do not assume port 8765.

### Checks and packaging

```powershell
uv sync --extra test
uv run python -m unittest discover -s tests -v
cd desktop
npm ci
npx install-electron
npm test
```

`tools/build_runtime.py` builds runtime components without the four optional models. Configure real download URLs with `tools/configure_release.py`, then build with Electron Builder. [PACKAGING.md](PACKAGING.md) covers platform requirements, and [STATUS.md](STATUS.md) records validation limits. Those detailed notes are currently in Chinese.
