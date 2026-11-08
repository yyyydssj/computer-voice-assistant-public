# 小饼·语音助手启动脚本（2026-10-23 固化；2026-10-24 人设文件驱动）
#
# 目的：每次不用手敲环境变量，一条命令进"语音 + 真实 DeepSeek Provider + 本机 VoiceStudio 播报"。
#
# 用法：
#   .\run-voice.ps1                        # 聊天模式（默认，不接任务执行）
#   .\run-voice.ps1 -TaskExec              # 接任务执行（打开白名单应用 calc/notepad）
#   .\run-voice.ps1 -Persona D:\我的.md     # 临时换一份人物模板（不改代码）
#
# 说明：
#   - 两个必设开关：ASSISTANT_PROVIDER=deepseek + ASSISTANT_DEEPSEEK_OPT_IN=true；
#     缺任一 ⇒ provider 回落 local ⇒ 回复是本地兜底（不可播报，语音轮只剩 text_only）。
#   - 播报链路默认 ws_pcm，需要本机 VoiceStudio（127.0.0.1:3900）；不可达时按
#     --tts-autostart on 自动拉起（实测冷启约 8 s 就绪），退出时只关自己拉起的进程。
#   - 控制台输出同时写一份到 %TEMP%\cva-voice-<时间戳>.log，便于事后对照日志。
#   - 人物模板：默认读 implementation_plan\persona.md，**换人设只改这个文件、重启生效**；
#     也可以用 -Persona（等价 --persona）或环境变量 ASSISTANT_PERSONA_PATH 指向别的模板。

param(
    [switch]$TaskExec,
    [string]$Persona
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 控制台/管道统一 UTF-8（2026-10-24 修 §5.1 的日志乱码）：
#   Python 侧强制 UTF-8 输出（PYTHONIOENCODING + UTF-8 模式），PowerShell 侧按 UTF-8 解码
#   原生命令输出（[Console]::OutputEncoding）。两边不一致时 `Tee-Object` 会把 UTF-8 字节
#   按 GBK 解码（或反之）⇒ 满屏 U+FFFD，无法逐句对照。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # 没有控制台（重定向/CI）时设不了，忽略即可；不影响下面的日志与运行。
}

$env:ASSISTANT_PROVIDER = 'deepseek'
$env:ASSISTANT_DEEPSEEK_OPT_IN = 'true'

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到虚拟环境 Python：$python"
}

$runArgs = @('-m', 'app.main', '--voice', '--auto')
if (-not $TaskExec) {
    $runArgs += '--no-task-exec'
}
$personaLabel = Join-Path $root 'implementation_plan\persona.md'
if ($Persona) {
    $runArgs += @('--persona', $Persona)
    $personaLabel = $Persona
}

$logPath = Join-Path $env:TEMP ('cva-voice-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

Write-Host ("启动：{0} {1}" -f $python, ($runArgs -join ' '))
Write-Host ("Provider：{0}（opt-in={1}）" -f $env:ASSISTANT_PROVIDER, $env:ASSISTANT_DEEPSEEK_OPT_IN)
Write-Host ("人物模板：{0}（换人设只改这个文件，重启生效；也可 -Persona 指定别的）" -f $personaLabel)
Write-Host ("控制台日志：{0}" -f $logPath)
Write-Host "提示：启动后等控制台出现「本机 TTS 服务已就绪」或「已自动启动本机 TTS 服务」再说话。"

& $python @runArgs 2>&1 | Tee-Object -FilePath $logPath
exit $LASTEXITCODE
