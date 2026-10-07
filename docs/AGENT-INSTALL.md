# 让 agent 安装 Lecture Transcribe

**简体中文** | [English](AGENT-INSTALL.en.md) · [返回首页](../README.md)

这份指南供能使用终端或桌面的 agent 执行。**下载安装器不算完成安装**：还需要基础运行环境，以及用户选择的模型。

## 先确定安装范围

- 当前安装包只适用于 **Windows x64**。macOS/Linux 按 [源码指南](DEVELOPMENT.md) 配置，不运行 Windows 安装器。
- 默认安装桌面应用和基础 CPU 环境，约 267 MB 下载量，不下载模型或 NVIDIA 组件。
- 用户要求实时双语、并同意模型下载时，安装 `asr-turbo` + `mt-live`，额外约 2.75 GB。照用户的模型选择执行，不重复请求已有授权。
- 未指定模型位置时使用应用默认目录并告知用户；若磁盘空间有限，先确定目录再下载大文件。
- 检查现有安装、快捷方式和进程，复用可用安装。不同时启动两份服务，不强制结束录音或删除用户资料。普通安装不使用开发选项 `LT_DEV_PYTHON`。

## 1. 下载并校验官方安装器

从官方最新稳定 Release 获取附件名称，不猜版本号。下面的 PowerShell 只下载和校验，不运行安装器；下载目录可换成用户指定的位置。遇到匿名 API 限流时，改用公开发行页面获取版本和校验清单，无需 GitHub Token。

```powershell
$ErrorActionPreference = 'Stop'
if (![Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::Windows) -or [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'This installer requires Windows x64.'
}
try {
    $release = Invoke-RestMethod 'https://api.github.com/repos/CercaTrovato/lecture-transcribe/releases/latest'
} catch {
    if ([int]$_.Exception.Response.StatusCode -notin @(403, 429)) { throw }
    $page = Invoke-WebRequest 'https://github.com/CercaTrovato/lecture-transcribe/releases/latest' -Method Head
    $uri = $page.BaseResponse.ResponseUri
    if (!$uri) { $uri = $page.BaseResponse.RequestMessage.RequestUri }
    if ($uri.AbsolutePath -notmatch '^/CercaTrovato/lecture-transcribe/releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)$') {
        throw 'Could not determine the official stable release.'
    }
    $tag = $Matches[1]
    $base = 'https://github.com/CercaTrovato/lecture-transcribe/releases/download/' + $tag + '/'
    $name = 'LectureTranscribe-Setup-' + $tag.Substring(1) + '.exe'
    $release = [pscustomobject]@{ assets = @(
        [pscustomobject]@{name=$name; browser_download_url=$base+$name; digest=$null},
        [pscustomobject]@{name='SHA256SUMS.txt'; browser_download_url=$base+'SHA256SUMS.txt'}
    ) }
}
$assets = @($release.assets | Where-Object name -Match '^LectureTranscribe-Setup-.*\.exe$')
if ($assets.Count -ne 1) { throw 'Expected one official Windows installer.' }
$asset = $assets[0]
$folder = Join-Path $env:USERPROFILE 'Downloads\LectureTranscribe'
New-Item -ItemType Directory -Force $folder | Out-Null
$installer = Join-Path $folder $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer
$expected = $asset.digest -replace '^sha256:', ''
if ($expected -notmatch '^[a-fA-F0-9]{64}$') {
    $sumAsset = $release.assets | Where-Object name -EQ 'SHA256SUMS.txt'
    $sums = [string](Invoke-RestMethod $sumAsset.browser_download_url)
    $pattern = '(?m)^([a-fA-F0-9]{64})\s+' + [regex]::Escape($asset.name) + '\s*$'
    if ($sums -notmatch $pattern) { throw 'Installer checksum missing.' }
    $expected = $Matches[1]
}
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expected) {
    throw 'Installer checksum mismatch. Do not run this file.'
}
Write-Output "Verified installer: $installer"
```

校验失败时停下并报告，不运行文件或关闭系统安全保护。当前安装器未签名，Windows 可能提示未知发布者。

## 2. 安装桌面应用

没有已有安装时，可操作安装界面，或使用当前用户的静默安装。下面沿用上一步已校验的 `$installer`，安装目录可修改；`/D=` 必须放在最后。

```powershell
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\LectureTranscribe'
$installedExe = Join-Path $installDir 'LectureTranscribe.exe'
$process = Start-Process -FilePath $installer -ArgumentList '/S', '/currentuser', "/D=$installDir" -WindowStyle Hidden -PassThru -Wait
if ($null -ne $process.ExitCode -and $process.ExitCode -ne 0) {
    throw "Installer failed: $($process.ExitCode)"
}
if (!(Test-Path -LiteralPath $installedExe)) { throw 'Installed application not found.' }
```

安装器会下载桌面应用，不必手动解压 Release 的 `.7z`。遇到网络或安全提示时，报告实际阻塞，不把下载成功当成安装成功。

## 3. 安装基础运行环境

**能操作桌面时：** 打开 `LectureTranscribe.exe`，点击「基础运行环境 → 下载并安装」，完成后点击「进入应用」。只有用户要求 NVIDIA 加速时，才安装选装组件。

**只能使用浏览器自动化时：** 新安装的应用支持本机 CDP 操作。先选择空闲的调试端口；下面使用 9229。不要重启正在使用的服务或启动第二份应用。

```powershell
Start-Process -FilePath $installedExe -ArgumentList '--headless', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9229' -WindowStyle Hidden
```

等待 `http://127.0.0.1:9229/json/version` 和应用页面就绪，用 agent 已有的 CDP/Playwright 能力连接。不要让普通用户为此额外安装开发环境。以下 JavaScript 例子适用于已有 Playwright 的 agent 环境：

```javascript
const { chromium } = await import('playwright');
const browser = await chromium.connectOverCDP('http://127.0.0.1:9229');
const page = browser.contexts()[0].pages()[0];
await page.waitForFunction(() => !!window.desktop);
const runtimes = await page.evaluate(() => window.desktop.runtimeStatus());
if (runtimes.error) throw new Error(runtimes.error);
const cpu = runtimes.components.find(c => c.id === 'cpu-win32-x64');
if (!cpu) throw new Error('Windows CPU runtime missing from manifest');
if (!cpu.installed) {
  const result = await page.evaluate(() => window.desktop.installRuntime('cpu-win32-x64'));
  if (!result.ok) throw new Error(result.error);
}
const launched = await page.evaluate(() => window.desktop.launch());
if (!launched.ok) throw new Error(launched.error);
await page.waitForURL('http://127.0.0.1:**');
const status = await page.evaluate(() => fetch('/api/status').then(r => r.json()));
console.log({ localURL: page.url(), status });
```

应用负责下载、校验和解压环境，不手工伪造安装凭据。后台端口可能改变，使用实际 `page.url()`，不假设是 8765。

## 4. 按用户选择安装模型

只安装程序的任务无需下载模型。已授权实时双语时，在「模型管理」下载 Turbo 和 1.8B，或从应用自己的本机页面调用以下接口：

- `GET /api/models`：包含 `id`、`installed`；下载时 `download` 中有 `state`、`bytes`、`total`、`error`。
- `POST /api/models/storage`，JSON 为 `{"path":"用户选择的绝对目录"}`：设置模型目录。不会移动旧文件，不用它整理用户已有资料。
- `POST /api/models/asr-turbo/download` 和 `POST /api/models/mt-live/download`：开始下载。先检查已安装状态，不重复下载。
- `POST /api/models/{id}/reference`，JSON 为 `{"path":"已有模型路径"}`：引用已有权重，避免重新下载。

写请求用应用页面中的 `fetch` 发出；JSON 请求设置 `Content-Type: application/json`，不用其他网站发请求。下载响应只表示已经开始；每隔几秒查看进度，直到所选模型的 `installed` 都为 `true`。`download.state === 'error'` 时报告具体错误；大文件下载期间说明进度。

NVIDIA 组件 ID 为 `cuda-win32-x64`，额外约 1.95 GB，只在用户要求时安装。之后再调用 `window.desktop.launch()`，让后台使用新环境。正在录音或处理时，等任务完成，不强行退出。

## 5. 怎样才算完成

1. 主界面确实能打开，`GET /api/status` 正常响应。
2. 只装基础环境时，四个模型可以都未安装。这是「仅录音模式」，不是转录已就绪。
3. 转录至少需要一个已安装的语音模型；实时翻译还需要 `mt-live`，状态中的 `mt.live_available` 为 `true`。
4. 不自动采集麦克风声音。推理测试使用用户允许的录音或自写音频；未运行推理，不声称识别和翻译已实测。
5. 告知安装位置、打开方法、模型和数据目录、实际下载内容，以及仍缺少模型的功能。

`--headless` 测试窗口不可见。确认录音、任务、导入和下载均空闲后，用应用窗口的 `window.close()` 安全退出，再正常打开 `LectureTranscribe.exe` 给用户使用。调试模式不作为日常入口。
