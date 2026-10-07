# Install Lecture Transcribe with an agent

[简体中文](AGENT-INSTALL.md) | **English** · [Back to the README](../README.en.md)

This guide is for agents with terminal or desktop access. **Downloading the installer alone is not a completed installation.** Set up the base runtime and any models chosen by the user.

## Establish the scope

- The installer supports **Windows x64**. For macOS/Linux, use the [source guide](DEVELOPMENT.md#english), not the Windows installer.
- Default to the desktop app and base CPU runtime, about 267 MB of downloads. Do not download models or NVIDIA acceleration by default.
- If the user requests live bilingual transcription and approves model downloads, install `asr-turbo` + `mt-live`, about 2.75 GB extra. Follow explicit choices without asking again for authorization already given.
- Use the default model folder if none is specified and report it. If storage is limited, settle the folder before downloading large files.
- Check existing installations, shortcuts, and processes. Reuse an available app. Do not run two services, interrupt active recordings, or delete user data. Normal installation does not use the development option `LT_DEV_PYTHON`.

## 1. Download and verify the official installer

Read the latest stable official Release rather than guessing its version. This PowerShell example only downloads and verifies. Replace the download folder if the user specifies one. If anonymous API access is rate-limited, it uses the public release page and checksum file instead; no GitHub token is required.

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

Stop and report a failed checksum. Do not run that file or disable OS security protections. The current installer is unsigned, so Windows may show an unknown publisher prompt.

## 2. Install the desktop app

If there is no existing installation, use the UI or a per-user silent install. This example uses the verified `$installer` from step 1. Adjust the installation folder as requested; `/D=` must be the last argument.

```powershell
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\LectureTranscribe'
$installedExe = Join-Path $installDir 'LectureTranscribe.exe'
$process = Start-Process -FilePath $installer -ArgumentList '/S', '/currentuser', "/D=$installDir" -WindowStyle Hidden -PassThru -Wait
if ($null -ne $process.ExitCode -and $process.ExitCode -ne 0) {
    throw "Installer failed: $($process.ExitCode)"
}
if (!(Test-Path -LiteralPath $installedExe)) { throw 'Installed application not found.' }
```

The installer downloads the desktop app; do not manually extract the Release `.7z`. Report network or security prompts accurately. Download success is not installation success.

## 3. Install the base runtime

**With desktop access:** open `LectureTranscribe.exe`, click **基础运行环境 → 下载并安装** (Base runtime → Download and install), then **进入应用** (Open app). Install NVIDIA acceleration only if requested.

**With browser automation only:** a new installation can be controlled over local CDP. Pick an unused debugging port; this example uses 9229. Do not restart an active service or launch a second copy.

```powershell
Start-Process -FilePath $installedExe -ArgumentList '--headless', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9229' -WindowStyle Hidden
```

Wait for `http://127.0.0.1:9229/json/version` and the app page to be ready. Connect using the agent's existing CDP/Playwright capability; do not require ordinary users to install a development environment. This JavaScript example uses an existing Playwright installation:

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

The app downloads, verifies, and extracts runtimes. Do not fabricate installation receipts. The backend port can change; use the actual `page.url()` instead of assuming port 8765.

## 4. Install only the chosen models

An app-only setup does not require models. If the user approves live bilingual transcription, download Turbo and 1.8B in **模型管理** (Model management), or use the same APIs from the application's local page:

- `GET /api/models`: includes `id` and `installed`; while downloading, `download` contains `state`, `bytes`, `total`, and `error`.
- `POST /api/models/storage` with JSON `{"path":"user-chosen absolute folder"}`: sets storage. It does not move old files; do not use it to reorganize existing data.
- `POST /api/models/asr-turbo/download` and `POST /api/models/mt-live/download`: starts downloading. Skip models already installed.
- `POST /api/models/{id}/reference` with JSON `{"path":"existing model path"}`: reuses existing weights without downloading them again.

Use `fetch` from the app's page for write requests, with `Content-Type: application/json` for JSON bodies; do not send them from unrelated websites. A response only means downloading started. Poll progress every few seconds until all chosen models have `installed: true`. Report `download.state === 'error'` and its error text. Keep the user informed during large downloads.

The NVIDIA runtime ID is `cuda-win32-x64`, about 1.95 GB extra; install it only if requested. Call `window.desktop.launch()` again afterward to use the new environment. Wait for recordings and tasks to finish rather than forcing them to stop.

## 5. Completion checks

1. The main interface opens and `GET /api/status` responds.
2. For an app-only setup, all four models may remain absent. This is recording-only mode, not transcription readiness.
3. Transcription needs at least one speech model. Live translation also needs `mt-live`, with `mt.live_available: true` in status.
4. Do not capture microphone audio automatically. Test inference with a permitted recording or self-written audio; if inference was not run, do not claim it was tested.
5. Report installation and data locations, how to open the app, actual downloads, and features still missing models.

A `--headless` test window is invisible. Once recording, jobs, imports, and downloads are idle, close it through the app's `window.close()`, then launch `LectureTranscribe.exe` normally for the user. Debug mode is not the everyday entry point.
