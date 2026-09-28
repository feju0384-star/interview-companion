# Bootstrap only this project's environment. No firewall or machine-wide changes.
param([switch]$Background, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Find-TaskPython {
    $taskCandidates = @((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
                        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
    $taskLauncher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($taskLauncher) {
        try {
            $taskResolved = & $taskLauncher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $taskResolved) { $taskCandidates += [string]$taskResolved }
        } catch { } # A launcher without 3.12 must fall through to installation.
    }
    $taskCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($taskCommand -and $taskCommand.Source -notlike '*\WindowsApps\*') { $taskCandidates += $taskCommand.Source }
    foreach ($taskCandidate in $taskCandidates) {
        if (-not (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) { continue }
        try {
            & $taskCandidate -I -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $taskCandidate }
        } catch { continue }
    }
    return $null
}

try {
    Write-Host '[1/3] 正在检查 Python 3.12（64 位）……'
    $taskPython = Find-TaskPython
    if ($CheckOnly) {
        if (-not $taskPython) { throw '尚未找到 Python 3.12（64 位）。' }
        Write-Host 'Python 已就绪。仅检查，未修改环境。'
        exit 0
    }
    if (-not $taskPython) {
        $taskWinget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $taskWinget) {
            throw '未找到 WinGet。请从 https://www.python.org/downloads/windows/ 安装 Python 3.12（64 位），再双击启动脚本。'
        }
        Write-Host '正在为当前 Windows 账户安装 Python。WinGet 可能提示确认软件源条款。'
        & $taskWinget.Source install --id Python.Python.3.12 --exact --source winget --scope user --architecture x64 --silent
        if ($LASTEXITCODE -ne 0) { throw 'Python 安装未完成，请查看上方 WinGet 提示后重试。' }
        $taskPython = Find-TaskPython
        if (-not $taskPython) { throw 'Python 已安装但尚未找到，请重新双击本脚本，或运行 py -3.12 launcher.py。' }
    }
    Write-Host '[2/3] 正在准备应用依赖并启动后台……'
    $taskArguments = @((Join-Path $PSScriptRoot 'launcher.py'))
    if ($Background) { $taskArguments += '--background' }
    & $taskPython @taskArguments
    if ($LASTEXITCODE -ne 0) { throw '应用启动未完成，请查看上方提示，检查网络后重试。原有设置会保留。' }
    Write-Host '[3/3] 已就绪。请在配对页检测设备，然后用手机扫码并配置模型。'
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
