param(
    [Parameter(Mandatory = $false)]
    [string]$Root = (Split-Path -Parent $PSScriptRoot),

    [Parameter(Position = 0)]
    [ValidateSet("menu", "start", "stop", "restart", "status", "open")]
    [string]$Action = "menu"
)

$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
$Python = Join-Path $Root "M1\.venv\Scripts\python.exe"
$Runtime = Join-Path $Root ".runtime"
$PidFile = Join-Path $Runtime "uvicorn.pid"
$StdOutLog = Join-Path $Runtime "uvicorn.out.log"
$StdErrLog = Join-Path $Runtime "uvicorn.err.log"
$HostAddress = "127.0.0.1"
$Port = 8000
$Url = "http://${HostAddress}:${Port}/"
$HealthUrl = "http://${HostAddress}:${Port}/api/health"

function Test-AppHealth {
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -Uri $HealthUrl
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-AppListener {
    $netstat = Join-Path $env:SystemRoot "System32\netstat.exe"
    foreach ($line in (& $netstat -ano -p TCP)) {
        if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            return [pscustomobject]@{
                OwningProcess = [int]$Matches[1]
            }
        }
    }
    return $null
}

function Test-AppIdentity {
    try {
        $metadata = Invoke-RestMethod `
            -TimeoutSec 2 `
            -Uri "http://${HostAddress}:${Port}/api/metadata"
        return (
            $metadata.p0_1_frozen -eq $true -and
            $metadata.financing_mode -eq "synthetic_stress"
        )
    }
    catch {
        return $false
    }
}

function Test-ProjectProcess {
    param([int]$ProcessId)

    try {
        $process = Get-CimInstance `
            -ClassName Win32_Process `
            -Filter "ProcessId=$ProcessId" `
            -ErrorAction Stop
    }
    catch {
        return $false
    }
    if ($null -eq $process) {
        return $false
    }
    $commandLine = [string]$process.CommandLine
    return (
        $commandLine -match "backend\.api\.main:app" -and
        $commandLine -match "uvicorn"
    )
}

function Start-App {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host "[错误] 找不到 Python 环境：" -ForegroundColor Red
        Write-Host $Python
        return 1
    }
    if (Test-AppHealth) {
        Write-Host "[提示] 服务已经在运行。" -ForegroundColor Yellow
        Write-Host "地址：$Url"
        try {
            Start-Process $Url
        }
        catch {
            Write-Host "[提示] 自动打开浏览器失败，请手动访问上述地址。" -ForegroundColor Yellow
        }
        return 0
    }
    $listener = Get-AppListener
    if ($null -ne $listener) {
        Write-Host "[错误] 端口 $Port 已被进程 $($listener.OwningProcess) 占用。" -ForegroundColor Red
        Write-Host "为避免误杀其他程序，本项目不会自动关闭该进程。"
        return 2
    }

    New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
    $arguments = @(
        "-m",
        "uvicorn",
        "backend.api.main:app",
        "--host",
        $HostAddress,
        "--port",
        "$Port"
    )
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $arguments `
        -WorkingDirectory (Join-Path $Root "M2") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdOutLog `
        -RedirectStandardError $StdErrLog `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii

    $healthy = $false
    for ($index = 0; $index -lt 40; $index++) {
        if (Test-AppHealth) {
            $healthy = $true
            break
        }
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 400
    }

    if (-not $healthy) {
        Write-Host "[错误] 服务启动失败。" -ForegroundColor Red
        Write-Host "错误日志：$StdErrLog"
        if (Test-Path -LiteralPath $StdErrLog) {
            Get-Content -LiteralPath $StdErrLog -Tail 20
        }
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return 1
    }

    Write-Host "[成功] 服务已启动。" -ForegroundColor Green
    Write-Host "地址：$Url"
    Write-Host "PID：$($process.Id)"
    Write-Host "日志：$StdOutLog"
    try {
        Start-Process $Url
    }
    catch {
        Write-Host "[提示] 自动打开浏览器失败，请手动访问上述地址。" -ForegroundColor Yellow
    }
    return 0
}

function Stop-App {
    $candidateIds = @()
    $savedIds = @()
    if (Test-Path -LiteralPath $PidFile) {
        try {
            $savedId = (Get-Content -LiteralPath $PidFile -Raw).Trim()
            if ($savedId) {
                $savedIds += [int]$savedId
                $candidateIds += [int]$savedId
            }
        }
        catch {
            Write-Host "[提示] PID 文件无法解析，将继续检查端口。" -ForegroundColor Yellow
        }
    }
    $listener = Get-AppListener
    $portOwner = $null
    if ($null -ne $listener) {
        $portOwner = [int]$listener.OwningProcess
        $candidateIds += [int]$listener.OwningProcess
    }
    $candidateIds = @($candidateIds | Sort-Object -Unique)

    $stopped = @()
    $appIdentity = Test-AppIdentity
    foreach ($candidateId in $candidateIds) {
        $process = Get-Process -Id $candidateId -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        $trustedSavedProcess = (
            $savedIds -contains $candidateId -and
            $process.ProcessName -eq "python" -and
            $appIdentity
        )
        $trustedPortProcess = (
            $candidateId -eq $portOwner -and
            $process.ProcessName -eq "python" -and
            $appIdentity
        )
        if (
            (Test-ProjectProcess -ProcessId $candidateId) -or
            $trustedSavedProcess -or
            $trustedPortProcess
        ) {
            Stop-Process -Id $candidateId -Force
            $stopped += $candidateId
        }
    }

    for ($index = 0; $index -lt 20; $index++) {
        if ($null -eq (Get-AppListener)) {
            break
        }
        Start-Sleep -Milliseconds 200
    }

    if ($stopped.Count -gt 0) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "[成功] 已关闭本项目服务。" -ForegroundColor Green
        Write-Host "PID：$($stopped -join ", ")"
        return 0
    }

    $remaining = Get-AppListener
    if ($null -ne $remaining) {
        Write-Host "[错误] 端口仍被未知进程 $($remaining.OwningProcess) 占用。" -ForegroundColor Red
        Write-Host "为避免误杀其他程序，未执行关闭。"
        return 2
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "[提示] 服务当前未运行。" -ForegroundColor Yellow
    return 0
}

function Show-Status {
    $listener = Get-AppListener
    $healthy = Test-AppHealth
    if ($healthy) {
        Write-Host "[状态] 运行中" -ForegroundColor Green
    }
    elseif ($null -ne $listener) {
        Write-Host "[状态] 端口已监听，但健康检查失败" -ForegroundColor Red
    }
    else {
        Write-Host "[状态] 未运行" -ForegroundColor Yellow
    }
    Write-Host "地址：$Url"
    if ($null -ne $listener) {
        Write-Host "监听 PID：$($listener.OwningProcess)"
    }
    else {
        Write-Host "监听 PID：无"
    }
    if (Test-Path -LiteralPath $PidFile) {
        Write-Host "脚本 PID 文件：$((Get-Content -LiteralPath $PidFile -Raw).Trim())"
    }
    Write-Host "输出日志：$StdOutLog"
    Write-Host "错误日志：$StdErrLog"
    if (Test-Path -LiteralPath $StdErrLog) {
        $errors = Get-Content -LiteralPath $StdErrLog -Tail 20 |
            Where-Object {
                $_ -match "ERROR|CRITICAL|Traceback|Exception"
            } |
            Select-Object -Last 8
        if ($errors) {
            Write-Host ""
            Write-Host "最近错误日志："
            $errors
        }
    }
    if ($healthy) {
        return 0
    }
    return 1
}

function Open-App {
    try {
        Start-Process $Url
    }
    catch {
        Write-Host "[提示] 自动打开浏览器失败，请手动访问 $Url" -ForegroundColor Yellow
    }
    return 0
}

function Invoke-Action {
    param([string]$SelectedAction)

    switch ($SelectedAction) {
        "start" { return Start-App }
        "stop" { return Stop-App }
        "status" { return Show-Status }
        "open" { return Open-App }
        "restart" {
            $stopCode = Stop-App
            if ($stopCode -ne 0) {
                return $stopCode
            }
            Start-Sleep -Milliseconds 600
            return Start-App
        }
        default { return 2 }
    }
}

if ($Action -ne "menu") {
    exit (Invoke-Action -SelectedAction $Action)
}

while ($true) {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  ETF 分层 AI 与极端杠杆压力测试系统" -ForegroundColor White
    Write-Host "  快速启动 / 关闭" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  [1] 启动服务并打开网页"
    Write-Host "  [2] 关闭服务"
    Write-Host "  [3] 重启服务"
    Write-Host "  [4] 查看运行状态"
    Write-Host "  [5] 仅打开网页"
    Write-Host "  [0] 退出"
    Write-Host ""
    $choice = Read-Host "请选择"
    switch ($choice) {
        "1" { $code = Invoke-Action "start" }
        "2" { $code = Invoke-Action "stop" }
        "3" { $code = Invoke-Action "restart" }
        "4" { $code = Invoke-Action "status" }
        "5" { $code = Invoke-Action "open"; continue }
        "0" { exit 0 }
        default {
            Write-Host "[提示] 请输入 0-5。" -ForegroundColor Yellow
            $code = 0
        }
    }
    Write-Host ""
    Read-Host "按 Enter 返回菜单" | Out-Null
}
