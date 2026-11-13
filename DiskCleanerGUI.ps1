# DiskCleanerGUI.ps1
# WinForms 图形界面扫盘清理工具(本地运行, 可真正清理缓存; 复用安全白名单与删除红线)。
# 兼容 Windows PowerShell 5.1 / pwsh 7。无第三方依赖。
# 关键设计: 耗时的目录大小统计放到后台 Runspace 线程, UI 线程只负责轮询结果与刷新界面,
# 因此扫描/统计期间窗口保持"响应"、按钮可点、可切换标签; 清理动作带忙锁与分步日志刷新。
# 用法:
#   双击启动器, 或: powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleanerGUI.ps1
#   无窗口只读自检(验证采集逻辑,不删除): powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleanerGUI.ps1 -SelfTest
#
# 安全边界: 默认只勾选可再生 L1 缓存; 回收站 / gradle caches / AVD 默认不勾选且执行前弹窗二次确认;
# 个人文件、WinSxS、Installer、页面/休眠文件、ProgramData/Roaming 整体、安全与系统软件永不进入可清理列表。
#requires -Version 5.1
param([switch]$SelfTest)

$ErrorActionPreference = 'SilentlyContinue'

# ==================== 自包含采集脚本块: SelfTest 与后台线程共用同一份口径 ====================
$script:CollectBlock = {
    param([string]$Mode)
    $ErrorActionPreference = 'SilentlyContinue'
    function DirMB($path) {
        if (-not (Test-Path -LiteralPath $path)) { return 0 }
        [math]::Round(((Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB)
    }
    function Get-BrowserTargets {
        $list = New-Object System.Collections.Generic.List[object]
        foreach ($ud in @("$env:LOCALAPPDATA\Google\Chrome\User Data", "$env:LOCALAPPDATA\Microsoft\Edge\User Data")) {
            if (-not (Test-Path -LiteralPath $ud)) { continue }
            foreach ($prof in (Get-ChildItem -LiteralPath $ud -Directory -ErrorAction SilentlyContinue)) {
                if ($prof.Name -ne 'Default' -and $prof.Name -notlike 'Profile *') { continue }
                foreach ($sub in @('Cache', 'Code Cache')) {
                    $p = Join-Path $prof.FullName $sub
                    if (Test-Path -LiteralPath $p) { $list.Add([PSCustomObject]@{ Name = "浏览器缓存 $($prof.Name) $sub"; Path = $p }) }
                }
            }
        }
        return $list
    }

    if ($Mode -eq 'fast') {
        $d = @{}
        $c = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
        $d.CFreeGB = [math]::Round($c.FreeSpace / 1GB, 2); $d.CTotalGB = [math]::Round($c.Size / 1GB, 1)
        $dd = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='D:'"
        if ($dd) { $d.DFreeGB = [math]::Round($dd.FreeSpace / 1GB, 2) } else { $d.DFreeGB = -1 }

        $safe = New-Object System.Collections.Generic.List[object]
        $safeTargets = @(
            @('用户临时文件 %TEMP%', $env:TEMP),
            @('系统临时文件 Windows\Temp', 'C:\Windows\Temp'),
            @('Windows 更新缓存', 'C:\Windows\SoftwareDistribution\Download'),
            @('D3D 着色器缓存', "$env:LOCALAPPDATA\D3DSCache"),
            @('NVIDIA DX 缓存', "$env:LOCALAPPDATA\NVIDIA\DXCache"),
            @('NVIDIA GL 缓存', "$env:LOCALAPPDATA\NVIDIA\GLCache"),
            @('系统日志 Windows\Logs', 'C:\Windows\Logs'),
            @('错误报告 WER', "$env:ProgramData\Microsoft\Windows\WER"),
            @('蓝屏小转储 Minidump', 'C:\Windows\Minidump')
        )
        foreach ($t in $safeTargets) { $safe.Add([PSCustomObject]@{ Name = $t[0]; Path = $t[1]; MB = (DirMB $t[1]) }) }
        foreach ($b in (Get-BrowserTargets)) { $safe.Add([PSCustomObject]@{ Name = $b.Name; Path = $b.Path; MB = (DirMB $b.Path) }) }
        $thumb = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\Windows\Explorer" -Filter 'thumbcache_*' -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        if ($thumb) { $safe.Add([PSCustomObject]@{ Name = '缩略图缓存 thumbcache'; Path = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"; MB = [math]::Round($thumb / 1MB); IsFileThumb = $true }) }
        $d.Safe = $safe
        return $d
    }
    else {
        $ex = @{}
        $rb = (New-Object -ComObject Shell.Application).NameSpace(0xA).Items() | Measure-Object Size -Sum
        if ($rb.Sum) { $ex.RecycleMB = [math]::Round($rb.Sum / 1MB) } else { $ex.RecycleMB = 0 }
        $ex.GradleCachesMB = DirMB 'C:\Users\Abnny\.gradle\caches'
        $avd = New-Object System.Collections.Generic.List[object]
        $base = 'C:\Users\Abnny\.android\avd'
        if (Test-Path -LiteralPath $base) {
            foreach ($dir in (Get-ChildItem -LiteralPath $base -Directory -Filter '*.avd' -Force)) {
                $name = $dir.Name -replace '\.avd$', ''
                $avd.Add([PSCustomObject]@{ Name = $name; MB = (DirMB $dir.FullName) })
            }
        }
        $ex.Avd = $avd
        $ex.SdkMB = DirMB "$env:LOCALAPPDATA\Android\Sdk"
        $ex.WrapperMB = DirMB 'C:\Users\Abnny\.gradle\wrapper'
        $bigDirs = New-Object System.Collections.Generic.List[object]
        foreach ($s in (Get-ChildItem -LiteralPath $env:USERPROFILE -Directory -Force)) {
            $bigDirs.Add([PSCustomObject]@{ MB = (DirMB $s.FullName); Name = $s.Name })
        }
        $bigFiles = New-Object System.Collections.Generic.List[object]
        foreach ($folder in @("$env:USERPROFILE\Downloads", "$env:USERPROFILE\Desktop", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Videos")) {
            if (-not (Test-Path -LiteralPath $folder)) { continue }
            foreach ($f in (Get-ChildItem -LiteralPath $folder -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 300MB })) {
                $bigFiles.Add([PSCustomObject]@{ MB = [math]::Round($f.Length / 1MB); Path = $f.FullName; Date = $f.LastWriteTime })
            }
        }
        $ex.BigDirs = ($bigDirs | Sort-Object MB -Descending | Select-Object -First 10)
        $ex.BigFiles = ($bigFiles | Sort-Object MB -Descending | Select-Object -First 15)
        return $ex
    }
}

function Build-ReportText($fast, $extra) {
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("==== 磁盘扫描报告 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====")
    $lines.Add("C: 总 $($fast.CTotalGB) GB / 可用 $($fast.CFreeGB) GB ; D: 可用 $($fast.DFreeGB) GB")
    $sumSafe = 0
    $lines.Add('-- 可安全清理 L1(可再生) --')
    foreach ($s in $fast.Safe) { $sumSafe += $s.MB; $lines.Add(("{0,8} MB  {1}" -f $s.MB, $s.Name)) }
    $lines.Add(("L1 合计约 {0:N2} GB" -f ($sumSafe / 1024)))
    $rbMB = if ($extra) { $extra.RecycleMB } else { 0 }
    $lines.Add(("回收站 {0} MB(默认不清,清空不可恢复)" -f $rbMB))
    $lines.Add("---- 高级占用(只读/需确认) ----")
    $wrapMB = if ($extra) { $extra.WrapperMB } else { 0 }
    $sdkMB = if ($extra) { $extra.SdkMB } else { 0 }
    $gradleMB = if ($extra) { $extra.GradleCachesMB } else { 0 }
    $lines.Add("gradle caches $gradleMB MB ; wrapper 保留 $wrapMB MB")
    $lines.Add("Android SDK $sdkMB MB")
    if ($extra -and $extra.Avd) { foreach ($a in $extra.Avd) { $lines.Add(("AVD {0}: {1} MB" -f $a.Name, $a.MB)) } }
    if ($extra) {
        $lines.Add('-- 占用大户: 主目录一级目录 Top10 --')
        foreach ($b in $extra.BigDirs) { $lines.Add(("{0,8} MB  {1}" -f $b.MB, $b.Name)) }
        $lines.Add('-- 个人目录大文件 >300MB(只读,自行决定) --')
        foreach ($f in $extra.BigFiles) { $lines.Add(("{0,8} MB  {1}  ({2:yyyy-MM-dd})" -f $f.MB, $f.Path, $f.Date)) }
    }
    return ($lines -join [Environment]::NewLine)
}

# ==================== SelfTest: 无窗口只读验证(直接在当前会话执行采集块) ====================
if ($SelfTest) {
    $fast = & $script:CollectBlock 'fast'
    $extra = & $script:CollectBlock 'extra'
    Write-Output (Build-ReportText $fast $extra)
    Write-Output "SELFTEST_OK no window, no deletion"
    return
}

# ==================== 删除动作(UI 会话; 带安全约束) ====================
function DirMB($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    [math]::Round(((Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB)
}
function Get-FreeGB { [math]::Round((Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace / 1GB, 2) }
function Remove-CacheContent($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $before = DirMB $path
    Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $after = DirMB $path
    return [math]::Round($before - $after)
}
function Stop-GradleDaemon {
    $found = New-Object System.Collections.Generic.List[string]
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
        Where-Object { $_.CommandLine -match 'org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $found.Add("PID=$($_.ProcessId)") }
    return ($found -join ', ')
}
function Remove-AvdByName($name) {
    $base = 'C:\Users\Abnny\.android\avd'
    $avd = Join-Path $base "$name.avd"; $ini = Join-Path $base "$name.ini"
    $mb = DirMB $avd
    if (Test-Path -LiteralPath $avd) { Remove-Item -LiteralPath $avd -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $ini) { Remove-Item -LiteralPath $ini -Force -ErrorAction SilentlyContinue }
    return $mb
}

# ==================== 自绘控件库(C# 5 兼容, 无第三方依赖) ====================
$script:UiLib = @"
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace DCLib
{
    public static class Native
    {
        [DllImport("user32.dll")] public static extern bool ReleaseCapture();
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
        public static void Drag(Form f)
        {
            ReleaseCapture();
            SendMessage(f.Handle, 0x00A1, (IntPtr)0x2, IntPtr.Zero);
        }
    }

    public static class Shapes
    {
        public static GraphicsPath Rounded(Rectangle b, int r)
        {
            GraphicsPath p = new GraphicsPath();
            int d = r * 2;
            if (d < 1) d = 1;
            p.AddArc(b.X, b.Y, d, d, 180, 90);
            p.AddArc(b.Right - d, b.Y, d, d, 270, 90);
            p.AddArc(b.Right - d, b.Bottom - d, d, d, 0, 90);
            p.AddArc(b.X, b.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }
    }

    public class ModernForm : Form
    {
        public int Radius = 12;
        public ModernForm()
        {
            FormBorderStyle = FormBorderStyle.None;
            DoubleBuffered = true;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
        }
        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ClassStyle |= 0x00020000;
                return cp;
            }
        }
        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            using (GraphicsPath p = Shapes.Rounded(new Rectangle(0, 0, Width, Height), Radius))
            {
                Region old = Region;
                Region = new Region(p);
                if (old != null) old.Dispose();
            }
        }
        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (GraphicsPath p = Shapes.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), Radius))
            using (Pen pen = new Pen(Color.FromArgb(223, 228, 236), 1))
            {
                e.Graphics.DrawPath(pen, p);
            }
        }
    }

    public class CardPanel : Panel
    {
        public int Radius = 10;
        public Color BorderColor = Color.FromArgb(229, 233, 240);
        public CardPanel()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
        }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle b = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath p = Shapes.Rounded(b, Radius))
            using (SolidBrush br = new SolidBrush(BackColor))
            {
                e.Graphics.FillPath(br, p);
            }
            using (GraphicsPath p = Shapes.Rounded(b, Radius))
            using (Pen pen = new Pen(BorderColor, 1))
            {
                e.Graphics.DrawPath(pen, p);
            }
        }
    }

    public class PillLabel : Label
    {
        public int Radius = 11;
        public PillLabel()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
            TextAlign = ContentAlignment.MiddleCenter;
        }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            int r = Math.Min(Radius, Height / 2);
            if (r < 1) r = 1;
            using (GraphicsPath p = Shapes.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), r))
            using (SolidBrush br = new SolidBrush(BackColor))
            {
                e.Graphics.FillPath(br, p);
            }
            TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(0, 0, Width, Height), ForeColor,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
        }
    }

    public class ModernButton : Button
    {
        public int Radius = 8;
        public string Glyph = "";
        public Color BorderColor = Color.Empty;
        public Color HoverBackColor = Color.Empty;
        public Color PressBackColor = Color.Empty;
        public Color HoverForeColor = Color.Empty;
        private bool _hover;
        private bool _press;
        private Font _glyphFont;

        public ModernButton()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            FlatAppearance.MouseDownBackColor = Color.Transparent;
            FlatAppearance.MouseOverBackColor = Color.Transparent;
            Cursor = Cursors.Hand;
            _glyphFont = new Font("Segoe MDL2 Assets", 10.5f);
        }
        protected override void OnMouseEnter(EventArgs e) { _hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { _hover = false; _press = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { if (e.Button == MouseButtons.Left) { _press = true; Invalidate(); } base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { _press = false; Invalidate(); base.OnMouseUp(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color bg = BackColor;
            Color fg = ForeColor;
            if (!Enabled)
            {
                bg = Color.FromArgb(236, 240, 244);
                fg = Color.FromArgb(156, 163, 175);
            }
            else if (_press && PressBackColor != Color.Empty) { bg = PressBackColor; }
            else if (_hover && HoverBackColor != Color.Empty) { bg = HoverBackColor; if (HoverForeColor != Color.Empty) fg = HoverForeColor; }
            else if (_press) { bg = ControlPaint.Dark(BackColor, 0.12f); }
            else if (_hover) { bg = ControlPaint.Dark(BackColor, 0.06f); if (HoverForeColor != Color.Empty) fg = HoverForeColor; }

            Rectangle b = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath p = Shapes.Rounded(b, Radius))
            using (SolidBrush br = new SolidBrush(bg))
            {
                e.Graphics.FillPath(br, p);
            }
            if (BorderColor != Color.Empty)
            {
                using (GraphicsPath p = Shapes.Rounded(b, Radius))
                using (Pen pen = new Pen(BorderColor, 1))
                {
                    e.Graphics.DrawPath(pen, p);
                }
            }

            bool hasGlyph = (Glyph != null && Glyph.Length > 0);
            if (hasGlyph && Text != null && Text.Length > 0)
            {
                Size gs = TextRenderer.MeasureText(e.Graphics, Glyph, _glyphFont);
                Size ts = TextRenderer.MeasureText(e.Graphics, Text, Font);
                int total = gs.Width + 10 + ts.Width;
                int x0 = (Width - total) / 2;
                TextRenderer.DrawText(e.Graphics, Glyph, _glyphFont, new Rectangle(x0 - 4, 0, gs.Width + 8, Height), fg,
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
                TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(x0 + gs.Width + 10, 0, ts.Width + 12, Height), fg,
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            }
            else if (hasGlyph)
            {
                TextRenderer.DrawText(e.Graphics, Glyph, _glyphFont, new Rectangle(0, 0, Width, Height), fg,
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            }
            else
            {
                TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(0, 0, Width, Height), fg,
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            }
        }
    }
}
"@

# ==================== WinForms 界面 ====================
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not ('DCLib.ModernForm' -as [type])) {
    Add-Type -TypeDefinition $script:UiLib -ReferencedAssemblies System.Windows.Forms, System.Drawing
}
[System.Windows.Forms.Application]::EnableVisualStyles()

# 仅隐藏 PowerShell 控制台宿主窗口; WinForms 主窗体由 ShowDialog 独立创建显示, 不受影响。
# 注意: 不要对启动进程使用 -WindowStyle Hidden, 那会通过 STARTUPINFO 把主窗体一并置为不可见。
if (-not $SelfTest) {
    Add-Type -Name NativeConsole -Namespace DiskCleanerHost -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);
"@
    $consoleHwnd = [DiskCleanerHost.NativeConsole]::GetConsoleWindow()
    if ($consoleHwnd -ne [IntPtr]::Zero) { [void][DiskCleanerHost.NativeConsole]::ShowWindow($consoleHwnd, 0) }
}

$script:fast = $null
$script:extra = $null
$script:busy = $false
$script:extraLoaded = $false
$script:extraActive = $false
$script:job = $null

# ---------- 视觉主题: 中性浅灰底 + 白色卡片 + 翡翠绿主色, 克制留白 ----------
$script:C_Bg      = [System.Drawing.Color]::FromArgb(242,244,248)
$script:C_Card    = [System.Drawing.Color]::White
$script:C_Ink     = [System.Drawing.Color]::FromArgb(17,24,39)
$script:C_Sub     = [System.Drawing.Color]::FromArgb(107,114,128)
$script:C_Line    = [System.Drawing.Color]::FromArgb(229,233,240)
$script:C_Primary = [System.Drawing.Color]::FromArgb(14,138,107)
$script:C_PrimarySoft = [System.Drawing.Color]::FromArgb(230,244,238)
$script:C_Ok      = [System.Drawing.Color]::FromArgb(15,138,99)
$script:C_Warn    = [System.Drawing.Color]::FromArgb(217,119,6)
$script:C_Danger  = [System.Drawing.Color]::FromArgb(220,38,38)
$script:C_LogBg   = [System.Drawing.Color]::FromArgb(250,251,252)
$script:C_LogInk  = [System.Drawing.Color]::FromArgb(75,85,99)

function New-Font($size, $bold) {
    if ($bold) { return New-Object System.Drawing.Font('Microsoft YaHei UI', $size, [System.Drawing.FontStyle]::Bold) }
    return New-Object System.Drawing.Font('Microsoft YaHei UI', $size)
}
function Set-DblBuf($c) {
    $p = $c.GetType().GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'NonPublic,Instance')
    if ($p) { $p.SetValue($c, $true, $null) }
}

function Set-CleanButton($b, [string]$kind) {
    switch ($kind) {
        'primary' {
            $b.BackColor = $script:C_Primary; $b.ForeColor = [System.Drawing.Color]::White
            $b.BorderColor = [System.Drawing.Color]::Empty
            $b.Font = New-Font 9.5 $true
        }
        'secondary' {
            $b.BackColor = $script:C_Card; $b.ForeColor = $script:C_Ink
            $b.BorderColor = $script:C_Line
            $b.Font = New-Font 9.5 $false
        }
        'ghost' {
            $b.BackColor = $script:C_Bg; $b.ForeColor = $script:C_Sub
            $b.BorderColor = [System.Drawing.Color]::Empty
            $b.Font = New-Font 9.5 $false
        }
    }
}

# ---------- 主窗体: 无边框圆角 + 系统投影 ----------
$script:form = New-Object DCLib.ModernForm
$script:form.Text = '磁盘清理工具 DiskCleaner'
$script:form.ClientSize = New-Object System.Drawing.Size(1000, 712)
$script:form.StartPosition = 'CenterScreen'
$script:form.Font = New-Font 9 $false
$script:form.BackColor = $script:C_Bg
$script:form.MaximizeBox = $false

# ---------- 标题栏(可拖动) ----------
$caption = New-Object System.Windows.Forms.Panel
$caption.Location = New-Object System.Drawing.Point(0, 0); $caption.Size = New-Object System.Drawing.Size(1000, 52)
$caption.BackColor = $script:C_Bg
$script:form.Controls.Add($caption)

$icon = New-Object DCLib.CardPanel
$icon.Location = New-Object System.Drawing.Point(18, 14); $icon.Size = New-Object System.Drawing.Size(24, 24)
$icon.Radius = 7; $icon.BackColor = $script:C_Primary; $icon.BorderColor = $script:C_Primary
$caption.Controls.Add($icon)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Location = New-Object System.Drawing.Point(52, 8); $lblTitle.Size = New-Object System.Drawing.Size(300, 24)
$lblTitle.Text = '磁盘清理工具'; $lblTitle.BackColor = $script:C_Bg; $lblTitle.ForeColor = $script:C_Ink
$lblTitle.Font = New-Font 11 $true
$caption.Controls.Add($lblTitle)

$lblSubTitle = New-Object System.Windows.Forms.Label
$lblSubTitle.Location = New-Object System.Drawing.Point(53, 31); $lblSubTitle.Size = New-Object System.Drawing.Size(380, 16)
$lblSubTitle.Text = 'DiskCleaner  ·  只清可再生缓存,不碰个人文件'
$lblSubTitle.BackColor = $script:C_Bg; $lblSubTitle.ForeColor = $script:C_Sub
$lblSubTitle.Font = New-Font 8.5 $false
$caption.Controls.Add($lblSubTitle)

$script:lblStatus = New-Object DCLib.PillLabel
$script:lblStatus.Location = New-Object System.Drawing.Point(782, 15); $script:lblStatus.Size = New-Object System.Drawing.Size(96, 23)
$script:lblStatus.Radius = 11; $script:lblStatus.BackColor = $script:C_Ok
$script:lblStatus.ForeColor = [System.Drawing.Color]::White
$script:lblStatus.Font = New-Font 8.5 $true
$script:lblStatus.Text = '就绪'
$caption.Controls.Add($script:lblStatus)

$script:btnMin = New-Object DCLib.ModernButton
$script:btnMin.Location = New-Object System.Drawing.Point(898, 12); $script:btnMin.Size = New-Object System.Drawing.Size(44, 28)
$script:btnMin.Text = '—'; $script:btnMin.Radius = 6
$script:btnMin.Font = New-Font 11 $false
$script:btnMin.BackColor = $script:C_Bg; $script:btnMin.ForeColor = $script:C_Sub
$script:btnMin.HoverBackColor = [System.Drawing.Color]::FromArgb(228,232,238)
$caption.Controls.Add($script:btnMin)

$script:btnClose = New-Object DCLib.ModernButton
$script:btnClose.Location = New-Object System.Drawing.Point(944, 12); $script:btnClose.Size = New-Object System.Drawing.Size(44, 28)
$script:btnClose.Text = '×'; $script:btnClose.Radius = 6
$script:btnClose.Font = New-Font 12 $false
$script:btnClose.BackColor = $script:C_Bg; $script:btnClose.ForeColor = $script:C_Sub
$script:btnClose.HoverBackColor = $script:C_Danger
$script:btnClose.HoverForeColor = [System.Drawing.Color]::White
$caption.Controls.Add($script:btnClose)

# 拖动窗口
$script:dragHandler = { try { [DCLib.Native]::Drag($script:form) } catch {} }
foreach ($c in @($caption, $icon, $lblTitle, $lblSubTitle)) { $c.Add_MouseDown($script:dragHandler) }

# ---------- 磁盘状态卡 ----------
function New-StatCard($x, $title) {
    $card = New-Object DCLib.CardPanel
    $card.Location = New-Object System.Drawing.Point($x, 64); $card.Size = New-Object System.Drawing.Size(316, 76)
    $card.Radius = 10; $card.BackColor = $script:C_Card; $card.BorderColor = $script:C_Line

    $bar = New-Object DCLib.CardPanel
    $bar.Location = New-Object System.Drawing.Point(16, 20); $bar.Size = New-Object System.Drawing.Size(4, 36)
    $bar.Radius = 2; $bar.BackColor = $script:C_Primary; $bar.BorderColor = $script:C_Primary
    $card.Controls.Add($bar)

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Location = New-Object System.Drawing.Point(32, 11); $lbl.Size = New-Object System.Drawing.Size(268, 18)
    $lbl.Text = $title; $lbl.ForeColor = $script:C_Sub; $lbl.Font = New-Font 8.5 $false
    $card.Controls.Add($lbl)

    $val = New-Object System.Windows.Forms.Label
    $val.Location = New-Object System.Drawing.Point(31, 31); $val.Size = New-Object System.Drawing.Size(270, 32)
    $val.Text = '——'; $val.ForeColor = $script:C_Ink; $val.Font = New-Font 15 $true
    $card.Controls.Add($val)

    $script:form.Controls.Add($card)
    return $val
}
$script:lblCVal = New-StatCard 16 'C 盘可用空间'
$script:lblDVal = New-StatCard 342 'D 盘可用空间'
$script:lblFreeVal = New-StatCard 668 'L1 可安全释放(勾选后)'

# ---------- 标签页 ----------
$script:tabs = New-Object System.Windows.Forms.TabControl
$script:tabs.Location = New-Object System.Drawing.Point(16, 152)
$script:tabs.Size = New-Object System.Drawing.Size(968, 382)
$script:tabs.Appearance = 'FlatButtons'
$script:tabs.DrawMode = 'OwnerDrawFixed'
$script:tabs.ItemSize = New-Object System.Drawing.Size(224, 32)
$script:tabs.BackColor = $script:C_Bg
$script:form.Controls.Add($script:tabs)
$script:tabs.Add_DrawItem({
    param($s, $e)
    $sel = ($e.Index -eq $s.SelectedIndex)
    $bg = if ($sel) { $script:C_Card } else { $script:C_Bg }
    $fg = if ($sel) { $script:C_Ink } else { $script:C_Sub }
    $br = New-Object System.Drawing.SolidBrush($bg); $e.Graphics.FillRectangle($br, $e.Bounds); $br.Dispose()
    $tb = $s.TabPages[$e.Index]
    $fnt = if ($sel) { (New-Font 9 $true) } else { (New-Font 9 $false) }
    # 注意: New-Object 的圆括号参数列表中逗号优先级高于加减号, 必须先算好坐标再传入
    $rx = $e.Bounds.X + 16; $ry = $e.Bounds.Y + 2; $rw = $e.Bounds.Width - 32; $rh = $e.Bounds.Height - 4
    $rect = New-Object System.Drawing.Rectangle($rx, $ry, $rw, $rh)
    [System.Windows.Forms.TextRenderer]::DrawText($e.Graphics, $tb.Text, $fnt, $rect, $fg, [System.Windows.Forms.TextFormatFlags]::Left -bor [System.Windows.Forms.TextFormatFlags]::VerticalCenter -bor [System.Windows.Forms.TextFormatFlags]::NoPadding -bor [System.Windows.Forms.TextFormatFlags]::NoPrefix -bor [System.Windows.Forms.TextFormatFlags]::SingleLine -bor [System.Windows.Forms.TextFormatFlags]::EndEllipsis)
    $fnt.Dispose()
    if ($sel) {
        $pen = New-Object System.Drawing.Pen($script:C_Primary, 3)
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $e.Graphics.DrawLine($pen, $e.Bounds.X + 16, $e.Bounds.Bottom - 2, $e.Bounds.X + 62, $e.Bounds.Bottom - 2)
        $pen.Dispose()
    }
})

$tabClean = New-Object System.Windows.Forms.TabPage('可清理项(勾选后清理)')
$tabBig = New-Object System.Windows.Forms.TabPage('占用大户(只读不删)')
$tabClean.BackColor = $script:C_Card; $tabBig.BackColor = $script:C_Card
[void]$script:tabs.TabPages.Add($tabClean)
[void]$script:tabs.TabPages.Add($tabBig)

$script:lv = New-Object System.Windows.Forms.ListView
$script:lv.Location = New-Object System.Drawing.Point(12, 12)
$script:lv.Size = New-Object System.Drawing.Size(650, 326)
$script:lv.View = 'Details'; $script:lv.CheckBoxes = $true; $script:lv.FullRowSelect = $true
$script:lv.GridLines = $false; $script:lv.HideSelection = $true
$script:lv.BorderStyle = 'None'; $script:lv.BackColor = $script:C_Card
$script:lv.ForeColor = $script:C_Ink; $script:lv.HeaderStyle = 'NonClickable'
$script:lv.Font = New-Font 9 $false
[void]$script:lv.Columns.Add('项目', 280); [void]$script:lv.Columns.Add('类别', 92); [void]$script:lv.Columns.Add('大小(MB)', 88); [void]$script:lv.Columns.Add('说明', 190)
Set-DblBuf $script:lv
$tabClean.Controls.Add($script:lv)

$panel = New-Object System.Windows.Forms.Panel
$panel.Location = New-Object System.Drawing.Point(674, 12)
$panel.Size = New-Object System.Drawing.Size(282, 326)
$panel.BackColor = $script:C_Card
$tabClean.Controls.Add($panel)

$script:btnScan = New-Object DCLib.ModernButton
$script:btnScan.Location = New-Object System.Drawing.Point(14, 12); $script:btnScan.Size = New-Object System.Drawing.Size(254, 40); $script:btnScan.Text = '刷新扫描'
Set-CleanButton $script:btnScan 'secondary'; $panel.Controls.Add($script:btnScan)

$script:btnSum = New-Object DCLib.ModernButton
$script:btnSum.Location = New-Object System.Drawing.Point(14, 60); $script:btnSum.Size = New-Object System.Drawing.Size(254, 40); $script:btnSum.Text = '统计选中大小'
Set-CleanButton $script:btnSum 'secondary'; $panel.Controls.Add($script:btnSum)

$script:btnClean = New-Object DCLib.ModernButton
$script:btnClean.Location = New-Object System.Drawing.Point(14, 108); $script:btnClean.Size = New-Object System.Drawing.Size(254, 54); $script:btnClean.Text = '清理选中项'
Set-CleanButton $script:btnClean 'primary'; $panel.Controls.Add($script:btnClean)

$script:btnReport = New-Object DCLib.ModernButton
$script:btnReport.Location = New-Object System.Drawing.Point(14, 170); $script:btnReport.Size = New-Object System.Drawing.Size(254, 40); $script:btnReport.Text = '导出扫描报告'
Set-CleanButton $script:btnReport 'secondary'; $panel.Controls.Add($script:btnReport)

$script:btnExit = New-Object DCLib.ModernButton
$script:btnExit.Location = New-Object System.Drawing.Point(14, 218); $script:btnExit.Size = New-Object System.Drawing.Size(254, 40); $script:btnExit.Text = '退出'
Set-CleanButton $script:btnExit 'ghost'; $panel.Controls.Add($script:btnExit)

$lblHint = New-Object System.Windows.Forms.Label
$lblHint.Location = New-Object System.Drawing.Point(14, 266); $lblHint.Size = New-Object System.Drawing.Size(254, 58)
$lblHint.BackColor = $script:C_Card; $lblHint.ForeColor = $script:C_Sub
$lblHint.Font = New-Font 8.5 $false
$lblHint.Text = '· L1 可再生缓存,默认勾选。' + [Environment]::NewLine +
 '· 回收站 / gradle / AVD 需二次确认。' + [Environment]::NewLine +
 '· 个人文件与系统组件永不列入。' + [Environment]::NewLine +
 '· 被占用文件自动跳过,窗口全程可操作。'
$panel.Controls.Add($lblHint)

$script:lvBig = New-Object System.Windows.Forms.ListView
$script:lvBig.Location = New-Object System.Drawing.Point(12, 12)
$script:lvBig.Size = New-Object System.Drawing.Size(944, 326)
$script:lvBig.View = 'Details'; $script:lvBig.FullRowSelect = $true; $script:lvBig.HideSelection = $true
$script:lvBig.GridLines = $false; $script:lvBig.BorderStyle = 'None'
$script:lvBig.BackColor = $script:C_Card; $script:lvBig.ForeColor = $script:C_Ink
$script:lvBig.HeaderStyle = 'NonClickable'
$script:lvBig.Font = New-Font 9 $false
[void]$script:lvBig.Columns.Add('类别', 150); [void]$script:lvBig.Columns.Add('大小(MB)', 92); [void]$script:lvBig.Columns.Add('名称 / 路径', 702)
Set-DblBuf $script:lvBig
$tabBig.Controls.Add($script:lvBig)

# ---------- 运行日志 ----------
$lblLog = New-Object System.Windows.Forms.Label
$lblLog.Location = New-Object System.Drawing.Point(16, 544); $lblLog.Size = New-Object System.Drawing.Size(300, 18)
$lblLog.Text = '运行日志'; $lblLog.ForeColor = $script:C_Sub
$lblLog.Font = New-Font 8.5 $true
$script:form.Controls.Add($lblLog)

$logCard = New-Object DCLib.CardPanel
$logCard.Location = New-Object System.Drawing.Point(16, 566); $logCard.Size = New-Object System.Drawing.Size(968, 130)
$logCard.Radius = 10; $logCard.BackColor = $script:C_Card; $logCard.BorderColor = $script:C_Line
$script:form.Controls.Add($logCard)

$script:logBox = New-Object System.Windows.Forms.TextBox
$script:logBox.Location = New-Object System.Drawing.Point(10, 10)
$script:logBox.Size = New-Object System.Drawing.Size(948, 110)
$script:logBox.Multiline = $true; $script:logBox.ReadOnly = $true; $script:logBox.ScrollBars = 'Vertical'
$script:logBox.BackColor = $script:C_LogBg; $script:logBox.ForeColor = $script:C_LogInk
$script:logBox.BorderStyle = 'None'
$script:logBox.Font = New-Object System.Drawing.Font('Consolas', 9)
$logCard.Controls.Add($script:logBox)

# ---------- 界面辅助 ----------
function Add-Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    $script:logBox.AppendText($line + [Environment]::NewLine)
    [System.Windows.Forms.Application]::DoEvents()
}
function Set-Busy([bool]$on, [string]$text) {
    $script:busy = $on
    $script:btnScan.Enabled = (-not $on)
    $script:btnClean.Enabled = (-not $on)
    $script:btnSum.Enabled = (-not $on)
    if ($on) {
        foreach ($b in @($script:btnScan, $script:btnSum, $script:btnClean)) {
            $b.BackColor = $script:C_Line; $b.ForeColor = $script:C_Sub; $b.BorderColor = [System.Drawing.Color]::Empty
        }
    }
    else {
        Set-CleanButton $script:btnScan 'secondary'
        Set-CleanButton $script:btnSum 'secondary'
        Set-CleanButton $script:btnClean 'primary'
    }
    $script:lblStatus.Text = $text
    if ($on) { $script:lblStatus.BackColor = $script:C_Warn } else { $script:lblStatus.BackColor = $script:C_Ok }
    $script:lblStatus.ForeColor = [System.Drawing.Color]::White
    [System.Windows.Forms.Application]::DoEvents()
}
# 只读补充统计: 只改状态文字, 不锁定动作按钮
function Set-StatusOnly([string]$text, [bool]$warn) {
    $script:lblStatus.Text = $text
    if ($warn) { $script:lblStatus.BackColor = $script:C_Warn } else { $script:lblStatus.BackColor = $script:C_Ok }
    $script:lblStatus.ForeColor = [System.Drawing.Color]::White
    [System.Windows.Forms.Application]::DoEvents()
}

function Update-StatCards {
    if ($script:fast) {
        $script:lblCVal.Text = ("{0} GB" -f $script:fast.CFreeGB)
        if ($script:fast.DFreeGB -ge 0) { $script:lblDVal.Text = ("{0} GB" -f $script:fast.DFreeGB) } else { $script:lblDVal.Text = '未检测到' }
    }
    $sumSafe = 0
    if ($script:fast) {
        $sumSafe = ($script:fast.Safe | Measure-Object MB -Sum).Sum
        if (-not $sumSafe) { $sumSafe = 0 }
    }
    $script:lblFreeVal.Text = ("{0:N2} GB" -f ($sumSafe / 1024))
}

function Populate-CleanList {
    $script:lv.Items.Clear()
    foreach ($s in $script:fast.Safe) {
        $kind = 'L1 安全缓存'; $note = '可再生,系统/构建自动重建'
        if ($s.IsFileThumb) { $note = '自动重建' }
        $item = New-Object System.Windows.Forms.ListViewItem($s.Name)
        [void]$item.SubItems.Add($kind); [void]$item.SubItems.Add([string]$s.MB); [void]$item.SubItems.Add($note)
        $item.Tag = @{ Action = 'CleanDir'; Path = $s.Path; IsThumb = [bool]$s.IsFileThumb }
        $item.Checked = $true
        [void]$script:lv.Items.Add($item)
    }
    # 高级项(回收站/gradle/AVD)在后台 extra 统计就绪后追加; 未就绪前不阻塞首屏
    if ($script:extra) {
        $rbItem = New-Object System.Windows.Forms.ListViewItem('清空 C 盘回收站')
        [void]$rbItem.SubItems.Add('不可逆'); [void]$rbItem.SubItems.Add([string]$script:extra.RecycleMB); [void]$rbItem.SubItems.Add('D盘回收站不动;清空不可恢复')
        $rbItem.Tag = @{ Action = 'RecycleC' }; $rbItem.Checked = $false
        [void]$script:lv.Items.Add($rbItem)

        $gItem = New-Object System.Windows.Forms.ListViewItem('gradle caches 构建缓存')
        [void]$gItem.SubItems.Add('可再生'); [void]$gItem.SubItems.Add([string]$script:extra.GradleCachesMB); [void]$gItem.SubItems.Add('下次构建重新下载依赖;保留wrapper')
        $gItem.Tag = @{ Action = 'GradleCaches' }; $gItem.Checked = $false
        [void]$script:lv.Items.Add($gItem)

        foreach ($a in $script:extra.Avd) {
            $ai = New-Object System.Windows.Forms.ListViewItem("AVD: $($a.Name)")
            [void]$ai.SubItems.Add('镜像/不可逆'); [void]$ai.SubItems.Add([string]$a.MB); [void]$ai.SubItems.Add('删除后需重建并下载系统镜像')
            $ai.Tag = @{ Action = 'Avd'; Name = $a.Name }; $ai.Checked = $false
            [void]$script:lv.Items.Add($ai)
        }
    }
    Update-StatCards
}

function Populate-BigList {
    $script:lvBig.Items.Clear()
    if (-not $script:extra) { return }
    foreach ($b in $script:extra.BigDirs) {
        $i = New-Object System.Windows.Forms.ListViewItem('主目录子目录')
        [void]$i.SubItems.Add([string]$b.MB); [void]$i.SubItems.Add($b.Name)
        [void]$script:lvBig.Items.Add($i)
    }
    foreach ($f in $script:extra.BigFiles) {
        $i = New-Object System.Windows.Forms.ListViewItem('个人目录大文件')
        [void]$i.SubItems.Add([string]$f.MB); [void]$i.SubItems.Add("$($f.Path)  ($($f.Date.ToString('yyyy-MM-dd')))")
        [void]$script:lvBig.Items.Add($i)
    }
    $sdk = New-Object System.Windows.Forms.ListViewItem('开发环境(不自动删)')
    [void]$sdk.SubItems.Add([string]$script:extra.SdkMB); [void]$sdk.SubItems.Add("Android SDK; wrapper $($script:extra.WrapperMB) MB")
    [void]$script:lvBig.Items.Add($sdk)
}

# ---------- 后台线程统计 + UI 定时器轮询(不阻塞消息循环) ----------
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 300

function Start-BackendScan([string]$mode) {
    if ($mode -eq 'fast') {
        if ($script:busy) { return }
        Set-Busy $true '扫描中…'
        Add-Log '后台开始扫描可清理项,界面保持可操作…'
    }
    else {
        if ($script:job -or $script:extraLoaded) { return }
        $script:extraActive = $true
        Set-StatusOnly '统计中…' $true
        Add-Log '后台补充统计 SDK/wrapper 与占用大户(只读,不影响操作)…'
    }
    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace()
    $rs.ApartmentState = 'STA'
    $rs.Open()
    $ps = [System.Management.Automation.PowerShell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($script:CollectBlock.ToString()).AddArgument($mode)
    $handle = $ps.BeginInvoke()
    $script:job = @{ ps = $ps; handle = $handle; rs = $rs; mode = $mode }
    $script:timer.Start()
}

function Poll-Backend {
    $j = $script:job
    if (-not $j) { $script:timer.Stop(); return }
    if (-not $j.handle.IsCompleted) { return }
    $script:timer.Stop()
    try {
        $out = $j.ps.EndInvoke($j.handle)
        $data = $out | Select-Object -First 1
        if ($j.mode -eq 'fast') {
            if ($null -ne $data) {
                $script:fast = $data
                Populate-CleanList
                Add-Log '可清理项扫描完成: L1默认勾选; 回收站/gradle/AVD默认未勾选。'
            }
        }
        else {
            if ($null -ne $data) {
                $script:extra = $data; $script:extraLoaded = $true; $script:extraActive = $false
                Populate-CleanList
                Populate-BigList
                Add-Log '高级项与占用大户统计完成: 回收站/gradle/AVD 已补充到清理列表(默认不勾选)。'
            }
        }
    }
    catch { Add-Log ("后台统计出错: " + $_.Exception.Message) }
    finally {
        try { $j.ps.Dispose() } catch {}
        try { $j.rs.Dispose() } catch {}
        $script:job = $null
        if ($j.mode -eq 'fast') {
            Set-Busy $false '就绪'
            Start-BackendScan 'extra'
        }
        else {
            $script:extraActive = $false
            Set-StatusOnly '就绪' $false
        }
    }
}
$script:timer.Add_Tick({ Poll-Backend })

function Get-CheckedActions {
    $acts = New-Object System.Collections.Generic.List[object]
    foreach ($it in $script:lv.CheckedItems) { if ($it.Tag) { $acts.Add($it.Tag) } }
    return $acts
}

# ---------- 执行清理(UI 线程, 忙锁 + 分步日志刷新) ----------
function Invoke-Clean {
    if ($script:busy) { return }
    if (-not $script:fast) { Add-Log '请先完成扫描再清理。'; return }
    $acts = Get-CheckedActions
    if ($acts.Count -eq 0) { [System.Windows.Forms.MessageBox]::Show('请先勾选至少一项。', '提示') | Out-Null; return }

    $hasRB = $false; $hasGradle = $false
    $avdNames = New-Object System.Collections.Generic.List[string]
    foreach ($a in $acts) {
        if ($a.Action -eq 'RecycleC') { $hasRB = $true }
        if ($a.Action -eq 'GradleCaches') { $hasGradle = $true }
        if ($a.Action -eq 'Avd') { $avdNames.Add($a.Name) }
    }
    if ($hasRB) {
        $r = [System.Windows.Forms.MessageBox]::Show('将清空 C 盘回收站(不可恢复;D盘回收站不动)。是否继续?', '确认回收站', 'YesNo', 'Warning')
        if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { Add-Log '用户取消了回收站清理。'; return }
    }
    if ($hasGradle) {
        $r = [System.Windows.Forms.MessageBox]::Show('将停止空闲 Gradle 守护进程并删除 caches;下次构建重新下载依赖。是否继续?', '确认 gradle 缓存', 'YesNo', 'Question')
        if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { Add-Log '用户取消了 gradle 缓存清理。'; return }
    }
    if ($avdNames.Count -gt 0) {
        $msg = '将删除以下 AVD 镜像(不可恢复):' + [Environment]::NewLine + ($avdNames -join [Environment]::NewLine) + [Environment]::NewLine + '是否继续?'
        $r = [System.Windows.Forms.MessageBox]::Show($msg, '确认删除 AVD', 'YesNo', 'Warning')
        if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { Add-Log '用户取消了 AVD 删除。'; return }
    }

    Set-Busy $true '清理中…'
    try {
        $beforeFree = Get-FreeGB
        Add-Log "清理开始, C盘清理前可用 $beforeFree GB"
        $freedMB = 0
        $wuWasRunning = (Get-Service wuauserv -ErrorAction SilentlyContinue).Status -eq 'Running'
        foreach ($a in $acts) {
            switch ($a.Action) {
                'CleanDir' {
                    if ($a.IsThumb) {
                        Get-ChildItem "$env:LOCALAPPDATA\Microsoft\Windows\Explorer" -Filter 'thumbcache_*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
                        Add-Log '已清理缩略图缓存(占用文件跳过)'
                    }
                    else {
                        if ($a.Path -eq 'C:\Windows\SoftwareDistribution\Download') {
                            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
                            $mb = Remove-CacheContent $a.Path
                            if ($wuWasRunning) { Start-Service wuauserv -ErrorAction SilentlyContinue }
                        }
                        else { $mb = Remove-CacheContent $a.Path }
                        $freedMB += $mb
                        Add-Log ("清理 {0}: 约 {1} MB" -f $a.Path, $mb)
                    }
                }
                'RecycleC' {
                    Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue
                    Add-Log 'C 盘回收站已清空(D盘未动)'
                }
                'GradleCaches' {
                    $pids = Stop-GradleDaemon
                    if ($pids) { Add-Log "已停空闲 Gradle 守护进程 $pids" }
                    Start-Sleep -Seconds 2
                    $mb = Remove-CacheContent 'C:\Users\Abnny\.gradle\caches'
                    $freedMB += $mb; Add-Log "gradle caches 释放约 $mb MB"
                }
                'Avd' {
                    $mb = Remove-AvdByName $a.Name
                    $freedMB += $mb; Add-Log "已删除 AVD $($a.Name): 约 $mb MB"
                }
            }
        }
        Start-Sleep -Seconds 2
        $afterFree = Get-FreeGB
        Add-Log ("清理完成: 累计动作释放约 {0:N2} GB; C盘可用 {1} GB -> {2} GB" -f ($freedMB / 1024), $beforeFree, $afterFree)
    }
    catch { Add-Log ("清理过程出错(部分项可能已完成): " + $_.Exception.Message) }
    finally {
        Set-Busy $false '就绪'
        Start-BackendScan 'fast'
    }
}

# ---------- 事件绑定(全部 try/catch 兜底, 避免事件异常导致界面假死) ----------
$script:btnMin.Add_Click({ try { $script:form.WindowState = 'Minimized' } catch {} })
$script:btnClose.Add_Click({ try { $script:form.Close() } catch {} })
$script:btnScan.Add_Click({ try { Start-BackendScan 'fast' } catch { Add-Log ("刷新出错: " + $_.Exception.Message) } })
$script:btnSum.Add_Click({
    try {
        if (-not $script:fast) { Add-Log '尚未扫描完成,请稍候再统计。'; return }
        $acts = Get-CheckedActions; $sum = 0
        foreach ($a in $acts) {
            switch ($a.Action) {
                'CleanDir' { foreach ($s in $script:fast.Safe) { if ($s.Path -eq $a.Path) { $sum += $s.MB } } }
                'RecycleC' { if ($script:extra) { $sum += $script:extra.RecycleMB } }
                'GradleCaches' { if ($script:extra) { $sum += $script:extra.GradleCachesMB } }
                'Avd' { if ($script:extra) { foreach ($x in $script:extra.Avd) { if ($x.Name -eq $a.Name) { $sum += $x.MB } } } }
            }
        }
        [System.Windows.Forms.MessageBox]::Show(("选中项合计约 {0:N2} GB。其中含不可逆项请留意确认弹窗。" -f ($sum / 1024)), '选中大小') | Out-Null
    } catch { Add-Log ("统计出错: " + $_.Exception.Message) }
})
$script:btnClean.Add_Click({ try { Invoke-Clean } catch { Add-Log ("清理出错: " + $_.Exception.Message); Set-Busy $false '就绪' } })
$script:btnReport.Add_Click({
    try {
        if (-not $script:fast) { Add-Log '尚未扫描完成,暂无可导出数据。'; return }
        $dlg = New-Object System.Windows.Forms.SaveFileDialog
        $dlg.Filter = '文本文件 (*.txt)|*.txt'; $dlg.FileName = "scan_report_$(Get-Date -Format yyyyMMdd_HHmm).txt"
        if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            Set-Content -LiteralPath $dlg.FileName -Value (Build-ReportText $script:fast $script:extra) -Encoding UTF8
            Add-Log "报告已导出: $($dlg.FileName)"
        }
    } catch { Add-Log ("导出出错: " + $_.Exception.Message) }
})
$script:btnExit.Add_Click({ $script:form.Close() })
$script:tabs.Add_SelectedIndexChanged({
    if ($script:tabs.SelectedIndex -eq 1) { Start-BackendScan 'extra' }
})

$script:form.Add_FormClosed({
    $script:timer.Stop()
    if ($script:job) {
        try { $script:job.ps.Stop() } catch {}
        try { $script:job.ps.Dispose() } catch {}
        try { $script:job.rs.Dispose() } catch {}
        $script:job = $null
    }
})

# 先显示界面并进入消息循环; 首扫在后台线程执行, UI 线程不被占用
$script:form.Add_Shown({
    $script:form.Refresh()
    $script:form.BeginInvoke([Action]{ Start-BackendScan 'fast' })
})
[void]$script:form.ShowDialog()
