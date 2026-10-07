# Lecture Transcribe

[简体中文](README.md) | **English**

Record a lecture, meeting, or conversation and see the words as you listen, with optional Chinese translation. You can also import existing audio or video and export timestamped transcripts, subtitles, and bilingual notes.

Speech recognition and translation run on your computer. Your recordings are not uploaded to a cloud service. Downloading the app and models requires internet access; once they are ready, you can use them locally without a cloud API key.

**[Download for Windows](https://github.com/CercaTrovato/lecture-transcribe/releases/latest)** · [Ask an agent to install it](#ask-an-agent-to-install-it) · [Choose your models](#choose-your-models) · [中文安装说明](README.md#在-windows-上安装)

## What you can do

- See recognized text while recording, and Chinese translation if you install a translation model.
- Import existing audio or video and transcribe the whole recording.
- Browse and play saved recordings, click text to jump to its timestamp, and search within the selected transcript.
- Export Markdown, TXT, SRT subtitles, or bilingual notes.
- Choose which models to install, where to store them, and when to remove them.

Without a model, you can still save recordings, import audio, and play saved files. Speech will not be converted to text until you install a speech recognition model.

## Install on Windows

The installer currently supports **Windows x64**. You do not need to install Python, Node.js, or the CUDA development toolkit first.

1. Open the **[download page](https://github.com/CercaTrovato/lecture-transcribe/releases/latest)**. Under **Assets**, download `LectureTranscribe-Setup-VERSION.exe`. The current version is [0.2.0 — direct download](https://github.com/CercaTrovato/lecture-transcribe/releases/download/v0.2.0/LectureTranscribe-Setup-0.2.0.exe). This `.exe` is the only file most users need to download manually.
2. Run it and choose an installation folder. The installer downloads the desktop app.
3. Open **LectureTranscribe**. Click **基础运行环境 → 下载并安装** (Base runtime → Download and install), then **进入应用** (Open app).
4. Open **模型管理** (Model management). Install **Whisper Turbo** to try speech recognition. Add **Hy-MT2 1.8B Q4** if you also want Chinese translation while recording.
5. Choose your microphone and speech language, enable real-time transcription or translation, then click **开始录音** (Start recording). After stopping, wait for the full transcript. Use **导入音频** (Import audio) to process an existing file.

**The current app interface is in Simplified Chinese.** This English guide includes the button labels you will see; it does not mean the interface has an English language switch.

App components download from GitHub; models download from Hugging Face. The current installer is unsigned, so Windows may show an unknown publisher prompt. Speed and accuracy depend on your hardware, recording quality, and speech.

### How much will it download?

These are **download sizes for 0.2.0**, not installed disk usage. Extraction and installation need additional free space.

| Item | Approximate download | When you need it |
| --- | ---: | --- |
| Installer `.exe` | 0.69 MB | Starts the app download |
| Desktop app | 114 MB | Downloaded by the installer |
| Base runtime | 153 MB | You install it on first launch |
| NVIDIA acceleration component | 1.95 GB | Optional, for NVIDIA GPU acceleration |
| Speech and translation models | See below | Choose according to your needs |

**The app and base runtime total about 267 MB of downloads. Models are not downloaded automatically.** Transcription and translation can run on CPU; speed depends on your computer. An NVIDIA GPU is optional.

## Choose your models

You do not need all four. Start with Turbo for text transcription, add 1.8B for live Chinese translation, and consider the other two later.

| Model | Approximate download | What it does | Without it |
| --- | ---: | --- | --- |
| **Whisper Turbo** | 1.62 GB | Everyday live and full-recording transcription | Use large-v3 instead; without either speech model, the app saves audio but cannot recognize words |
| Whisper large-v3 | 3.09 GB | An alternative speech model for trying higher accuracy | Turbo still works |
| **Hy-MT2 1.8B Q4** | 1.13 GB | Chinese translation while recording | Live original text, recording, and transcription still work |
| Hy-MT2 7B Q4 | 4.62 GB | A Chinese translation of the completed full transcript | No new final translation; original text and saved live bilingual notes remain |

**For live bilingual notes: Turbo + 1.8B, about 2.75 GB of model downloads.** If storage is limited, start with Turbo alone.

Model management shows each model's purpose, download progress, and removal controls. You can cancel or resume a download, or point the app to models you already have. Removing a model keeps your recordings and saved text. Removing an external reference leaves the original model file untouched. Models in use can only be removed after their tasks finish.

You can choose the model storage folder. Changing it does not move or delete files in the old folder.

## Ask an agent to install it

Copy this prompt to an agent that can use your computer's terminal or desktop. A chat assistant without computer access cannot install software for you.

> Follow https://github.com/CercaTrovato/lecture-transcribe/blob/main/docs/AGENT-INSTALL.en.md to install Lecture Transcribe. Check that this computer runs Windows x64. Download and verify the installer from the latest official Release, install the desktop app and base CPU runtime, and confirm that the main interface opens. Do not download models or the NVIDIA component by default. Reuse an existing installation and preserve my data. Report where the app is installed, how to open it, and which models are still missing.

To have the agent set up live bilingual transcription too, add:

> I approve downloading Whisper Turbo and Hy-MT2 1.8B Q4, about 2.75 GB in total. Store them in the folder I specify, and confirm both show as installed. Do not download large-v3, 7B, or the NVIDIA component.

See the [English agent installation guide](docs/AGENT-INSTALL.en.md) or [中文 agent 安装指南](docs/AGENT-INSTALL.md). These cover download verification, silent installation, runtime setup through the app, and completion checks.

## Where files are saved, and how to uninstall

On Windows, the desktop app stores recordings, transcripts, and models in `%APPDATA%\lecture-transcribe-desktop\data` by default. Runtimes are in the sibling `runtime` folder. You can choose a different model folder. **复制路径** (Copy path) shows the selected recording's location.

Uninstall LectureTranscribe from **Windows Settings → Apps → Installed apps**. Separately stored recordings, text, and models are kept. To free model storage, remove unwanted models in the app first.

## Live notes versus the full transcript

**Live bilingual notes** preserve what the app recognized and translated while you recorded. The **full transcript** is generated by processing the whole recording afterward and may correct recognition errors. With the 7B model and final translation enabled, you can also generate a translation aligned with that full transcript.

Choose the transcript type in the interface. Live translations are not attached to revised original text as if they were a new final translation.

## Other platforms and development

| Platform | Current status |
| --- | --- |
| Windows x64 | Installer released; tested in isolated folders on the development machine, including install, uninstall, and CPU/NVIDIA short samples |
| macOS / Linux | Source available; native installers are not released. Apple Metal still needs hardware validation |
| Phones and tablets | No native installer at present |

See the **[developer guide](docs/DEVELOPMENT.md#english)** for source setup and APIs, [validation status](docs/STATUS.md) for testing limits, and [packaging notes](docs/PACKAGING.md) for building release components. The detailed status and packaging notes are currently in Chinese.

Search terms: local transcription, speech-to-text, lecture recording, meeting notes, real-time bilingual transcription, offline translation, Whisper, Hy-MT, local AI.

Licensed under the [MIT License](LICENSE).
