# DiskCleaner

Windows 本地磁盘清理工具，基于 **PySide6** 的全盘文件扫描器。扫描所选磁盘的**全部文件与目录**（不只是安全缓存），按目录树展示并标注可删性，勾选后才执行清理。

> ⚠️ 本工具会**真实删除文件**，删除不可撤销。程序默认是只读扫描，只有你手动勾选并点击「清理」才会动手；执行前请逐项确认。

## 下载

- **当前版本：[直接下载 DiskCleaner-win64-v1.0.0.zip（45.4 MB）](https://github.com/abnnyf1-creator/DiskCleaner/releases/download/v1.0.0/DiskCleaner-win64-v1.0.0.zip)**
- [Releases 页面（查看全部版本）](https://github.com/abnnyf1-creator/DiskCleaner/releases/latest)
- [项目源码](https://github.com/abnnyf1-creator/DiskCleaner)

---

## 功能

- **全盘文件树**：列出所选磁盘的全部文件与目录，惰性分批加载，支持面包屑跳级、双击进入目录、一键折叠全部、关键字搜索。
- **三级可删性标注**：🟢 绿色 = 安全可删（缓存/临时/日志/构建产物）、🟠 橙色 = 谨慎删除（应用数据、依赖与包缓存）、🔴 红色 = 不可删除（系统与个人文件）。规则未命中的未知文件单列为「可疑」，标红但可单独筛选、交给 AI 判断。
- **磁盘卡即标签页**：顶部每个磁盘一张卡，点击切换下方页面；卡片右上角的「扫描」复选框决定是否纳入扫描范围。
- **筛选与勾选**：等级筛选、仅看已勾选、全选开关；全选旁的齿轮可设置全选覆盖的等级（绿/橙/红及常用预设）。勾选目录会自动包含其子孙。
- **AI 助手（可选）**：对接自定义 OpenAI 兼容 API，结论内联到「AI判断」列。不是逐文件提问，而是把全盘结果压成语义模式分组批量请求，配合本地缓存；点击前会弹出 **Token 预算确认**，缓存命中不消耗 token。

## 安全边界

- 绿色项默认勾选；橙色、红色、可疑项默认**不勾选**。
- 磁盘根目录、`Windows`、`WinSxS`、`Users`、`Program Files`、`pagefile.sys` / `hiberfil.sys` 等关键位置由 `is_blocked` 禁止删除。
- 个人文件与系统文件照常展示，但标红。
- 红色/可疑项即使被 AI 判为「安全可删」，也**不会自动勾选**，只在「AI判断」列给出建议，需你手动确认。
- 非绿色项清理前有二次确认。

## 运行

### 方式一：打包版（推荐给使用者）

[直接下载 v1.0.0 安装包](https://github.com/abnnyf1-creator/DiskCleaner/releases/download/v1.0.0/DiskCleaner-win64-v1.0.0.zip)，或从 [Releases 页面](https://github.com/abnnyf1-creator/DiskCleaner/releases/latest) 选择其他版本，解压后运行 `DiskCleaner/DiskCleaner.exe`。

- 这是 PyInstaller **onedir** 打包，必须把 `DiskCleaner.exe` 和 `_internal/` 保持在同一目录；**单独拷出 exe 无法启动**。
- 首次使用 AI 助手，在界面「AI 助手」里填写 `base_url` / `model` / `api_key`。配置经 Windows DPAPI 加密后保存为 exe 同目录的 `ai_assistant.cfg`，**该文件含密钥，不要提交到仓库或转发给他人**。不配置也能正常使用扫描与清理功能。

### 方式二：源码运行

环境：Windows + Python 3.12 + PySide6（项目开发时使用 PySide6 6.12）。除 PySide6 外只用标准库。

```bash
pip install PySide6
python disk_cleaner_gui.py
python disk_cleaner_gui.py --selftest   # 无窗口只读自检
```

仓库里的 `启动清理工具GUI.cmd` 是作者本机的双击快捷方式，**里面写死了作者机器上的 Python 路径**，换机器请改成自己的解释器，或直接使用上面的命令。

### 自行打包

```bash
pip install pyinstaller
pyinstaller DiskCleaner.spec
```

产物为 `dist/DiskCleaner/DiskCleaner.exe` 与 `dist/DiskCleaner/_internal/`，需整目录分发。源码运行时配置写在脚本所在目录，打包后写在 exe 所在目录（不会写进 `_internal`）。

## 运行期生成的文件

以下文件由程序在本机生成，均已被 `.gitignore` 排除，**请勿提交**：

| 文件 | 说明 |
| --- | --- |
| `ai_assistant.cfg` | DPAPI 加密的 API 配置（含密钥） |
| `ai_verdict_cache.json` | AI 判定缓存，按 `model::pattern` 复用 |
| `ui_prefs.json` | 界面偏好，例如全选范围 |
| `*.log` | 运行日志 |

## 仓库文件说明

| 文件 | 说明 |
| --- | --- |
| `disk_cleaner_gui.py` | **当前主程序**（PySide6 GUI） |
| `disk_cleaner.ico` | 程序图标 |
| `DiskCleaner.spec` | PyInstaller onedir 打包配置 |
| `启动清理工具GUI.cmd` | 本机 GUI 快捷启动器（含绝对路径，跨机器需修改） |
| `DiskCleaner.ps1`、`DiskCleanerGUI.ps1`、`启动DiskCleaner.cmd` | 旧版 PowerShell 命令行 / WinForms GUI，**仅作备份，不再维护** |
| `DiskCleaner 使用说明.md`、`DiskCleaner GUI 使用说明.md` | 旧版文档，描述的是 PowerShell/WinForms 版本，**不适用于当前 PySide6 界面** |
| `C盘清理报告_2026-11-09.md` | 一次本机清理记录 |
| `_shot.py` | 窗口截图辅助脚本 |

## 注意

- 清理系统级目录可能需要管理员权限；以管理员身份运行时会看到更多系统文件，请格外谨慎。
- AI 助手只是辅助判断，最终删除决定始终由用户手动勾选并确认；作者不对误删造成的数据损失负责。
