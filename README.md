# Lecture Transcribe

本地录音、转录和可选双语翻译。所有模型由用户主动下载、引用或卸载，默认不会下载模型。

项目仓库：https://github.com/CercaTrovato/lecture-transcribe

## 使用方式

打开应用后，可以直接录音保存、导入音频、播放和查看历史；使用语音识别或翻译功能前，在「模型管理」中安装所需模型。

| 模型 | 权重大小约 | 作用 | 缺失影响 |
| --- | ---: | --- | --- |
| Whisper Turbo | 1.62 GB | 标准实时及完整转录共用 | 需要其他已安装的转录模型；没有转录模型时不识别语音 |
| Whisper large-v3 | 3.09 GB | 高精度转录选装包 | Turbo 标准转录仍可使用 |
| Hy-MT2 1.8B Q4 | 1.13 GB | 实时译文 | 只显示实时原文，不影响录音和转录 |
| Hy-MT2 7B Q4 | 4.62 GB | 最终整篇精译选装包 | 实时双语稿保留，不生成最终精译稿 |

标准双语推荐 Turbo + 1.8B，模型约 2.75 GB。推荐不等于自动安装。用户可以选择模型存储目录；更换目录不搬移或删除原文件。

下载支持校验、取消、同版本续传和重试。取消后的下载文件可主动清理。模型正在被任务使用时拒绝卸载；外部模型只移除引用，不删除文件。卸载模型不删除录音、转录或已有译文。

「实时双语稿」保留当时的源句和译文；「完整转录稿」是对整段音频重新识别的原文。两者可能不同，不将旧译文冒充为最终稿的精译。安装 7B 后可生成最终双语稿。

## 当前验证范围

Windows 的共享前端、模型管理、CUDA Turbo 和 CPU 1.8B 实时翻译已经验证。Apple Metal 适配器与 Linux/macOS 包装代码已接入，但仍需对应平台及硬件验收，不能视为已验证的正式跨平台发行版。

`release/` 中的 Windows 在线安装器是本地验证样包，其组件地址指向本机测试服务器。发布前必须按真实托管地址重新配置和构建；尚未上传公开仓库或 Release。

## 源码运行

需要 Python 3.12 和 uv：

```powershell
uv sync --extra test
# 普通安装不包含 NVIDIA 依赖；需要 CUDA 时主动选择附加组件：
uv sync --extra cuda --extra test
uv run app.py
```

macOS 源码模式需 Metal 绑定：`uv sync --extra metal --extra test`。翻译运行程序由安装器组件提供；源码模式可设置 `LT_LLAMA_SERVER` 指向已有的 `llama-server`。

Linux 源码模式还需系统 PortAudio 库，例如 Ubuntu/Debian 使用 `sudo apt-get install libportaudio2`；发行安装包应声明该系统依赖。

可选环境变量：

- `LT_DATA_DIR`：用户录音与配置目录。
- `LT_MODEL_DIR`：模型目录；也可在界面内选择并保存。
- `LT_DEVICE`：`auto`、`cpu`、`cuda` 或 `metal`。
- `LT_PORT`、`LT_MT_PORT`：本机服务端口。

Windows 默认数据目录为用户 LocalAppData 下的 `LectureTranscribe`；macOS 为 Application Support；Linux 为 XDG 用户数据目录。数据与源码、应用程序分离。

模型管理 API：`GET /api/models`，`POST /api/models/{id}/download`、`cancel`、`reference`，`DELETE /api/models/{id}`，`DELETE /api/models/{id}/partial`，`POST /api/models/storage`。本机接口拒绝其他网站触发的写操作。

## 验证与构建

```powershell
uv run python -m unittest discover -s tests -v
cd desktop
npm ci
npx install-electron
npm test
```

组件构建工具为 `tools/build_runtime.py`，需要目标平台的便携 Python、依赖目录和 llama.cpp 二进制。它不包含模型。用 `tools/configure_release.py --base-url <真实下载地址> --output <构建配置>` 设置发行地址，再使用 Electron Builder 构建；始终使用 `--publish never` 做本地验证。

完整验收状态见 [docs/STATUS.md](docs/STATUS.md)。
