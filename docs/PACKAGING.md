# 在线安装组件格式

前端、Electron 主进程与 Python 业务源文件共用一套。每个平台构建自己的运行二进制组件，不将模型纳入安装包。

## 层次

1. Windows NSIS Web 入口下载 Electron 应用组件；当前样包入口不足 1 MB。
2. 应用第一次启动显示统一初次配置页，用户点击下载基础 Python/音频/推理运行组件。
3. NVIDIA 运行组件另行选装；CPU 组件足以启动录音、模型管理及 CPU 推理。
4. 在模型管理中，每个模型单独由用户下载或引用。安装器和运行组件都不获取权重。

`components.json` 包含 `schema: 1`、`base_url` 和 `components`。组件键为 `cpu-win32-x64`、`cuda-win32-x64` 等；每项包含相对 `url`、精确 `bytes`、`sha256` 以及组件内的 `entry`。

组件下载验证大小与 SHA-256，解压前拒绝路径逃逸和符号链接；安装到按摘要分开的不可变目录。断网可重试续传；系统不支持 Range 时从头下载。损坏文件校验失败不会执行。

## 构建

`tools/build_runtime.py` 接受目标平台的便携 Python 根目录、依赖目录和 llama.cpp 二进制目录，生成 `release/cpu-<平台>.zip` 和清单；`--cuda` 生成 GPU 附加组件。模型存在于依赖目录时拒绝构建。

每个平台需要在原生系统构建和验证。Apple 组件应包含启用 Metal 的 pywhispercpp 1.5.1 或更新兼容版本；仅完成包构建不能证明 GPU 推理可用。Linux 需要确保音频和 Electron 的系统库可用。Apple 的真实模型时间戳、长静音接续和长录音任务控制必须验收。

发布者把最终组件上传到明确的发行地址，运行：

```text
python tools/configure_release.py --base-url https://<发行地址>/ --output <构建配置.json>
cd desktop
npx electron-builder --publish never --config <构建配置.json>
```

这里不会自动上传、创建远程仓库、提交或推送。跨平台源码检查工作流也不执行发布操作。

本地样包使用 `http://127.0.0.1:8847/`，仅用于审阅。不要把它当作可在其他电脑直接下载组件的正式安装器。配置真实发行地址后需重新构建。

## 隔离检查

开发时可设置 `LT_USER_DATA`、`LT_DATA_DIR`、`LT_COMPONENT_DIR` 指向独立测试目录。`--headless --remote-debugging-port=<本机端口>` 可用来检查隐藏的 Electron 窗口，日常启动无需这些参数。

`LT_DEV_PYTHON` 仅用于源码调试，正常安装包使用下载的便携 Python，不依赖用户电脑上的 Python 或 CUDA 工具链。
