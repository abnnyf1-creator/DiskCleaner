# DiskCleaner.ps1 使用说明

一个零依赖、可复用的 Windows 扫盘清理工具：**先只读扫描和预览，确认后再清理**，兼容 Windows PowerShell 5.1 与 pwsh 7，无需安装任何第三方软件。

## 一、文件组成

| 文件 | 作用 |
|---|---|
| `DiskCleaner.ps1` | 工具本体（单文件，含安全白名单与删除红线） |
| `启动DiskCleaner.cmd` | 双击进入交互菜单的启动器（自动选择 pwsh 或 powershell） |

## 二、怎么运行

### 方式 A：双击启动（推荐普通使用）
双击 `启动DiskCleaner.cmd`，进入中文交互菜单。

### 方式 B：命令行
```powershell
# 交互菜单
powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1

# 只读扫描，不改任何文件，并把结果存成报告
powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -ScanOnly -ReportPath .\scan_report.txt

# 预览安全清理清单（只打印可释放大小，不删除）
powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -SafeClean -DryRun

# 直接执行安全缓存清理（L1）
powershell -NoProfile -ExecutionPolicy Bypass -File .\DiskCleaner.ps1 -SafeClean
```

> 非管理员运行时，用户态缓存（临时目录、浏览器缓存、着色器、缩略图）可正常清理；`Windows\Temp`、系统日志、更新缓存等系统级目录若无权限会自动跳过。需要清理这些项时，右键启动器“以管理员身份运行”。

## 三、菜单项说明

| 菜单 | 动作 | 是否删除 | 默认安全 |
|---|---|---|---|
| 1 | 只读扫描：磁盘占用 + L1 垃圾大小 + 高级占用报告 | 不删 | 只读 |
| 2 | DryRun 预览：列出安全清理项可释放大小 | 不删 | 只读 |
| 3 | 安全清理 L1 缓存（执行前要求输入 YES） | 删可再生缓存 | 安全 |
| 4 | 占用大户只读浏览：用户目录大文件、一级目录排行 | 不删 | 只读 |
| 5 | 高级：清理 `.gradle\caches`（先停空闲 Gradle 守护进程，要求 YES） | 删可再生缓存 | 需二次确认 |
| 6 | 高级：逐个确认删除 Android AVD 镜像（要求 YES，成对删 `.avd`/`.ini`） | 删镜像，不可逆 | 逐个确认 |
| 7 | 清空 **C 盘**回收站（要求 YES；D 盘回收站不动） | 不可逆 | 单独确认 |
| 8 | 把扫描结果保存为文本报告 | 不删 | 只读 |
| 0 | 退出 | — | — |

## 四、命令行参数

| 参数 | 含义 |
|---|---|
| `-ScanOnly` | 只做只读扫描后退出，不进入菜单、不删除 |
| `-SafeClean` | 执行 L1 安全缓存清理；通常配合 `-DryRun` 先预览 |
| `-DryRun` | 仅预览（须与 `-SafeClean` 一起使用），不执行删除 |
| `-ReportPath <路径>` | `-ScanOnly` 时把扫描报告写入指定文本文件 |

## 五、会清理什么（L1 白名单，均可再生）

- 用户临时文件 `%TEMP%`、系统临时 `C:\Windows\Temp`
- Windows 更新下载缓存 `SoftwareDistribution\Download`（清理前后保持更新服务原状态）
- D3D / NVIDIA DX / GL 着色器缓存
- `Windows\Logs`、错误报告 WER、`Minidump` 与 `MEMORY.DMP`
- 缩略图缓存 `thumbcache_*`
- Chrome / Edge 的 `Cache` 与 `Code Cache`（枚举 Default 与所有 `Profile *`，**只删缓存目录**）

## 六、内置删除红线（任何参数都不会越过）

- 个人文件：桌面、文档、下载、图片、视频目录内的文件（菜单 4 只列出大文件供你自行决定）
- `C:\Windows\WinSxS`、`C:\Windows\Installer`
- `pagefile.sys`、`hiberfil.sys`、`swapfile.sys`（不直接手删）
- `ProgramData`、`AppData\Roaming` 整体目录
- 安全软件、系统组件、驱动、运行库与已安装程序目录
- 浏览器的书签、历史、密码、Cookie（与缓存严格分开）

## 七、高级项注意事项

- **回收站（菜单 7）**：仅清空 C 盘；清空后不可恢复，请先确认没有想还原的文件。
- **gradle caches（菜单 5）**：删除后首次构建会重新下载依赖，首次编译偏慢；`wrapper\dists` 发行版不会被本工具删除。
- **Android AVD（菜单 6）**：删除后不可恢复，需在 AVD 管理器重建并重新下载系统镜像；只允许操作 `.android\avd` 下的白名单名称，且删除时同时移除对应 `.ini`。
- 浏览器或模拟器正在运行时，被占用的缓存文件会自动跳过，不会强杀进程、不会解锁强删。

## 八、恢复与回退

- L1 缓存、gradle caches 属于可再生内容，无需回滚；系统和构建过程会按需自动重建。
- 被停止的 Gradle 守护进程会在下次构建时自动重启，无需手动恢复。
- AVD 镜像、回收站内容一旦删除不可恢复，使用菜单 6 / 7 前请确认。

## 九、验证状态

- 脚本已通过 PowerShell 静态语法解析校验（0 错误），并以 `-ScanOnly` 只读模式实跑通过；只读模式不会删除任何文件。
