# SRA 启动脚本：确保后端配置存在，并以管理员权限启动前端
# 用法：双击项目根目录的 启动SRA.bat；或 powershell -File scripts\start_sra.ps1 [-NoLaunch]
param(
    [switch]$NoLaunch  # 仅写入配置，不启动程序（调试用）
)
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$exePath = Join-Path $projectRoot 'SRAFrontend\SRAFrontend.Desktop\bin\Debug\net10.0\SRA.exe'

if (-not (Test-Path $exePath)) {
    Write-Error "未找到 SRA.exe：$exePath（请先编译前端：dotnet build SRAFrontend/SRAFrontend.Desktop/SRAFrontend.Desktop.csproj）"
    exit 1
}

# 首次运行或设置被重置时，写入自定义后端配置（venv Python + main.py），
# 其余设置项由 SRA 启动时自动补全为默认值
$settingsDir = Join-Path $env:APPDATA 'SRA'
$settingsPath = Join-Path $settingsDir 'settings.json'
if (-not (Test-Path $settingsPath)) {
    New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null
    $config = [ordered]@{
        advanced = [ordered]@{
            'backend.launchArgs'                           = '--inline'
            'backend.remote.enabled'                       = $false
            'backend.remote.baseUrl'                       = 'http://localhost:5000'
            'developerMode.overlay'                        = $false
            'developerMode.enabled'                        = $true
            'developerMode.customBackend.enabled'          = $true
            'developerMode.customBackend.command'          = (Join-Path $projectRoot '.venv\Scripts\python.exe')
            'developerMode.customBackend.arguments'        = 'main.py'
            'developerMode.customBackend.workingDirectory' = $projectRoot
            'developerMode.saveOcrImage'                   = $false
        }
    }
    $json = ConvertTo-Json -InputObject $config -Depth 5
    [System.IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "已写入后端配置：$settingsPath"
}

# 产物比源码旧：说明上次只编译没生成 exe（输出被占用时会这样），启动的还是旧界面
$newestSource = Get-ChildItem -Path (Join-Path $projectRoot 'SRAFrontend') -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in '.cs', '.axaml' -and $_.FullName -notmatch '\\(bin|obj)\\' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newestSource -and $newestSource.LastWriteTime -gt (Get-Item $exePath).LastWriteTime) {
    Write-Host '警告：SRA.exe 比源码旧，当前启动的是旧版本（改动不会生效）。' -ForegroundColor Yellow
    Write-Host "  最新源码：$($newestSource.Name)  $($newestSource.LastWriteTime)" -ForegroundColor Yellow
    Write-Host '  请关掉正在运行的 SRA，然后执行：dotnet build SRAFrontend/SRAFrontend.Desktop/SRAFrontend.Desktop.csproj' -ForegroundColor Yellow
}

if ($NoLaunch) { return }

# SRA.exe 清单要求管理员权限，通过 RunAs 触发提权（本机 UAC 为"不提示直接提升"时不会弹窗）
Start-Process -Verb RunAs -FilePath $exePath -WorkingDirectory (Split-Path -Parent $exePath)
Write-Host "已启动 SRA：$exePath"
