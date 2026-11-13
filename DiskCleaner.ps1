# DiskCleaner.ps1
# 零依赖 PowerShell 扫盘清理工具: 先只读扫描/预览, 确认后再清理。兼容 Windows PowerShell 5.1 与 pwsh 7。
#
# 安全原则:
#   - 默认只读; 只有菜单选择或显式参数才删除, 且只删"可再生缓存/临时文件"。
#   - 绝不删除(内置红线, 任何参数都不绕过):
#       桌面/文档/下载/图片/视频内的个人文件; C:\Windows\WinSxS; C:\Windows\Installer;
#       pagefile.sys / hiberfil.sys / swapfile.sys; ProgramData 与 Roaming 整体目录;
#       安全软件、系统组件、驱动运行库与已安装程序目录。
#   - 回收站、gradle caches、Android AVD 属于"高级项/不可逆项", 默认不执行, 需显式二次确认。
#
# 交互用法(双击/菜单):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1
# 命令行只读扫描(不修改任何文件):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -ScanOnly -ReportPath .\scan_report.txt
# 命令行预览安全清理(不删除):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -SafeClean -DryRun
# 命令行执行安全缓存清理(L1):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -SafeClean
#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$ScanOnly,
    [switch]$DryRun,
    [switch]$SafeClean,
    [string]$ReportPath
)

$ErrorActionPreference = 'SilentlyContinue'
$script:freedMB = 0
$script:scanLines = New-Object System.Collections.Generic.List[string]

# ---------------- 基础辅助 ----------------
function Get-FreeGB {
    [math]::Round((Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace / 1GB, 2)
}
function DirMB($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    [math]::Round(((Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB)
}
function FileMB($path) {
    if (Test-Path -LiteralPath $path) { [math]::Round((Get-Item -LiteralPath $path -Force).Length / 1MB) } else { 0 }
}
function Scan-Line($m) { $script:scanLines.Add($m); Write-Host $m }

# 浏览器: 只收集 Cache 与 Code Cache, 绝不碰 History/Login Data/Bookmarks/Cookies
function Get-BrowserCacheTargets {
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($ud in @("$env:LOCALAPPDATA\Google\Chrome\User Data", "$env:LOCALAPPDATA\Microsoft\Edge\User Data")) {
        if (-not (Test-Path -LiteralPath $ud)) { continue }
        foreach ($prof in (Get-ChildItem -LiteralPath $ud -Directory -ErrorAction SilentlyContinue)) {
            if ($prof.Name -ne 'Default' -and $prof.Name -notlike 'Profile *') { continue }
            foreach ($sub in @('Cache', 'Code Cache')) {
                $p = Join-Path $prof.FullName $sub
                if (Test-Path -LiteralPath $p) { $list.Add([PSCustomObject]@{ Name = "$($prof.Name) $sub"; Path = $p }) }
            }
        }
    }
    return $list
}

# L1 安全白名单(可再生缓存)
function Get-SafeTargets {
    $t = New-Object System.Collections.Generic.List[object]
    $t.Add([PSCustomObject]@{ Name = '用户临时文件 %TEMP%';            Path = $env:TEMP })
    $t.Add([PSCustomObject]@{ Name = '系统临时文件 Windows\Temp';      Path = 'C:\Windows\Temp' })
    $t.Add([PSCustomObject]@{ Name = 'Windows 更新缓存';               Path = 'C:\Windows\SoftwareDistribution\Download' })
    $t.Add([PSCustomObject]@{ Name = 'D3D 着色器缓存';                Path = "$env:LOCALAPPDATA\D3DSCache" })
    $t.Add([PSCustomObject]@{ Name = 'NVIDIA DX 着色器缓存';           Path = "$env:LOCALAPPDATA\NVIDIA\DXCache" })
    $t.Add([PSCustomObject]@{ Name = 'NVIDIA GL 着色器缓存';           Path = "$env:LOCALAPPDATA\NVIDIA\GLCache" })
    $t.Add([PSCustomObject]@{ Name = '系统日志 Windows\Logs';          Path = 'C:\Windows\Logs' })
    $t.Add([PSCustomObject]@{ Name = '错误报告 WER';                  Path = "$env:ProgramData\Microsoft\Windows\WER" })
    $t.Add([PSCustomObject]@{ Name = '蓝屏小转储 Minidump';            Path = 'C:\Windows\Minidump' })
    foreach ($b in (Get-BrowserCacheTargets)) { $t.Add([PSCustomObject]@{ Name = "浏览器缓存 - $($b.Name)"; Path = $b.Path }) }
    return $t
}

# ---------------- 只读扫描 ----------------
function Invoke-Scan {
    $script:scanLines.Clear()
    Scan-Line '================ 磁盘扫描(只读,未删除任何内容) ================'
    Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
        Scan-Line ("{0} 总 {1:N1} GB / 可用 {2:N1} GB ({3}%)" -f $_.DeviceID, ($_.Size / 1GB), ($_.FreeSpace / 1GB), [math]::Round($_.FreeSpace / $_.Size * 100))
    }
    Scan-Line ''
    Scan-Line '---- 可安全清理(L1,可再生) ----'
    $safeTotal = 0
    foreach ($t in (Get-SafeTargets)) {
        $mb = DirMB $t.Path; $safeTotal += $mb
        Scan-Line ("{0,8} MB  {1}" -f $mb, $t.Name)
    }
    $thumb = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\Windows\Explorer" -Filter 'thumbcache_*' -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    if (-not $thumb) { $thumb = 0 }
    $thumbMB = [math]::Round($thumb / 1MB); $safeTotal += $thumbMB
    Scan-Line ("{0,8} MB  缩略图缓存 thumbcache" -f $thumbMB)
    $dmp = FileMB 'C:\Windows\MEMORY.DMP'
    if ($dmp -gt 0) { $safeTotal += $dmp; Scan-Line ("{0,8} MB  内存转储 MEMORY.DMP" -f $dmp) }
    Scan-Line ("---- 安全项合计约可释放: {0:N2} GB ----" -f ($safeTotal / 1024))
    Scan-Line ''
    $rb = (New-Object -ComObject Shell.Application).NameSpace(0xA).Items() | Measure-Object Size -Sum
    $rbMB = 0; if ($rb.Sum) { $rbMB = [math]::Round($rb.Sum / 1MB) }
    Scan-Line ("回收站: {0} MB (清空不可恢复,默认不清理,需菜单单独确认)" -f $rbMB)
    Scan-Line ''
    Scan-Line '---- 高级占用(只读报告,默认不删) ----'
    Scan-Line ("gradle caches: {0} MB ; wrapper 发行版(保留): {1} MB" -f (DirMB 'C:\Users\Abnny\.gradle\caches'), (DirMB 'C:\Users\Abnny\.gradle\wrapper'))
    if (Test-Path -LiteralPath 'C:\Users\Abnny\.android\avd') {
        Scan-Line 'Android AVD 镜像(逐个删除前需二次确认):'
        Get-ChildItem -LiteralPath 'C:\Users\Abnny\.android\avd' -Directory -Filter '*.avd' -Force | ForEach-Object {
            Scan-Line ("  {0,7} MB  {1}" -f (DirMB $_.FullName), $_.Name)
        }
    }
    Scan-Line ("Android SDK: {0} MB" -f (DirMB "$env:LOCALAPPDATA\Android\Sdk"))
    Scan-Line '=============================================================='
}

# ---------------- 清理逻辑 ----------------
function Clear-CacheDir($label, $path, [bool]$dry) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $before = DirMB $path
    if ($dry) { Write-Host "[预览] $label : $before MB ($path)"; return }
    Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $after = DirMB $path
    $delta = $before - $after; $script:freedMB += $delta
    Write-Host ("[完成] {0}: 释放 {1} MB (被占用文件已跳过)" -f $label, [math]::Round($delta))
}

function Invoke-SafeClean([bool]$dry) {
    $beforeFree = Get-FreeGB
    Write-Host "安全缓存清理开始 (DryRun=$dry)"
    $wuWasRunning = (Get-Service wuauserv -ErrorAction SilentlyContinue).Status -eq 'Running'
    if (-not $dry) { Stop-Service wuauserv -Force -ErrorAction SilentlyContinue }
    foreach ($t in (Get-SafeTargets)) { Clear-CacheDir $t.Name $t.Path $dry }
    if (-not $dry -and $wuWasRunning) { Start-Service wuauserv -ErrorAction SilentlyContinue }
    $thumbDir = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
    if (-not $dry) { Get-ChildItem $thumbDir -Filter 'thumbcache_*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue }
    if (-not $dry -and (Test-Path 'C:\Windows\MEMORY.DMP')) { Remove-Item 'C:\Windows\MEMORY.DMP' -Force -ErrorAction SilentlyContinue }
    if ($dry) { Write-Host "(预览模式,未删除任何文件)" }
    else { Write-Host ("完成: 本次释放约 {0:N2} GB; C盘可用 {1} GB -> {2} GB" -f ($script:freedMB / 1024), $beforeFree, (Get-FreeGB)) }
}

# ---------------- 高级项(默认不执行,显式确认) ----------------
function Stop-GradleDaemon {
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
        Where-Object { $_.CommandLine -match 'org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon' } |
        ForEach-Object { Write-Host "停止空闲 Gradle 守护进程 PID=$($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Manage-Avd {
    $base = 'C:\Users\Abnny\.android\avd'
    if (-not (Test-Path -LiteralPath $base)) { Write-Host '未找到 AVD 目录'; return }
    $dirs = Get-ChildItem -LiteralPath $base -Directory -Filter '*.avd' -Force
    if (-not $dirs) { Write-Host '没有 AVD 镜像'; return }
    $map = @{}; $i = 0
    Write-Host '现有 AVD 镜像:'
    foreach ($d in $dirs) {
        $i++; $name = $d.Name -replace '\.avd$', ''; $map[$i] = $name
        Write-Host ("  [{0}] {1}  ({2} MB)" -f $i, $name, (DirMB $d.FullName))
    }
    $ans = Read-Host '输入要删除的序号(多个用逗号分隔),直接回车取消'
    if ([string]::IsNullOrWhiteSpace($ans)) { Write-Host '已取消'; return }
    foreach ($token in ($ans -split ',')) {
        $token = $token.Trim()
        $idx = 0; if (-not [int]::TryParse($token, [ref]$idx)) { continue }
        if (-not $map.ContainsKey($idx)) { continue }
        $n = $map[$idx]
        $ok = Read-Host ("确认删除 {0}? 不可恢复,输入 YES" -f $n)
        if ($ok -ne 'YES') { Write-Host "跳过 $n"; continue }
        $avdDir = Join-Path $base "$n.avd"; $iniFile = Join-Path $base "$n.ini"
        if (Test-Path -LiteralPath $avdDir) { Remove-Item -LiteralPath $avdDir -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $iniFile) { Remove-Item -LiteralPath $iniFile -Force -ErrorAction SilentlyContinue }
        Write-Host "已删除 $n (.avd 与 .ini)"
    }
}

function Clear-CRBin {
    $ans = Read-Host '清空 C 盘回收站将不可恢复(D盘回收站不动),输入 YES 确认'
    if ($ans -ne 'YES') { Write-Host '已取消'; return }
    Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue
    Write-Host 'C 盘回收站已清空'
}

# ---------------- 只读占用大户浏览 ----------------
function Show-BigItems {
    Write-Host '---- 用户目录大文件 >300MB(只读,请自行决定) ----'
    foreach ($d in @("$env:USERPROFILE\Downloads", "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Videos")) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        Get-ChildItem -LiteralPath $d -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Length -gt 300MB } | Sort-Object Length -Descending | Select-Object -First 10 |
            ForEach-Object { Write-Host ("{0,7} MB  {1}  ({2:yyyy-MM-dd})" -f [math]::Round($_.Length / 1MB), $_.FullName, $_.LastWriteTime) }
    }
    Write-Host '---- 用户主目录一级目录 Top10(只读) ----'
    Get-ChildItem -LiteralPath $env:USERPROFILE -Directory -Force |
        ForEach-Object { [PSCustomObject]@{ MB = (DirMB $_.FullName); Name = $_.Name } } |
        Sort-Object MB -Descending | Select-Object -First 10 |
        ForEach-Object { Write-Host ("{0,7} MB  {1}" -f $_.MB, $_.Name) }
}

function Save-Report($path) {
    if (-not $script:scanLines.Count) { Invoke-Scan }
    Set-Content -LiteralPath $path -Value $script:scanLines -Encoding UTF8
    Write-Host "报告已保存: $path"
}

# ---------------- 交互菜单 ----------------
function Show-MenuLoop {
    while ($true) {
        Write-Host ''
        Write-Host '==== DiskCleaner 菜单 ===='
        Write-Host '1. 只读扫描(磁盘/安全垃圾/高级占用报告)'
        Write-Host '2. 预览安全清理清单(DryRun,不删除)'
        Write-Host '3. 安全清理 L1 缓存(执行前确认)'
        Write-Host '4. 占用大户只读浏览(大目录/大文件)'
        Write-Host '5. 高级: 清理 gradle caches(可再生,二次确认)'
        Write-Host '6. 高级: 逐个确认删除 Android AVD 镜像'
        Write-Host '7. 清空 C 盘回收站(不可逆,单独确认)'
        Write-Host '8. 把扫描结果保存为报告文件'
        Write-Host '0. 退出'
        $sel = Read-Host '请输入选项'
        switch ($sel) {
            '1' { Invoke-Scan }
            '2' { Invoke-SafeClean $true }
            '3' {
                $c = Read-Host '确认清理安全缓存?输入 YES 继续'
                if ($c -eq 'YES') { Invoke-SafeClean $false } else { Write-Host '已取消' }
            }
            '4' { Show-BigItems }
            '5' {
                $c = Read-Host '将停止空闲 Gradle 守护进程并删除 caches,下次构建重新下载依赖。输入 YES 继续'
                if ($c -eq 'YES') { Stop-GradleDaemon; Start-Sleep -Seconds 2; Clear-CacheDir 'gradle caches' 'C:\Users\Abnny\.gradle\caches' $false } else { Write-Host '已取消' }
            }
            '6' { Manage-Avd }
            '7' { Clear-CRBin }
            '8' {
                $p = Read-Host '报告保存路径(直接回车存为 当前目录\scan_report.txt)'
                if ([string]::IsNullOrWhiteSpace($p)) { $p = (Join-Path (Get-Location).Path 'scan_report.txt') }
                Save-Report $p
            }
            '0' { return }
            default { Write-Host '无效选项' }
        }
        Write-Host ''
    }
}

# ---------------- 入口分流 ----------------
if ($ScanOnly) {
    Invoke-Scan
    if ($ReportPath) { Save-Report $ReportPath }
    return
}
if ($SafeClean) {
    Invoke-SafeClean $DryRun.IsPresent
    return
}
Show-MenuLoop
