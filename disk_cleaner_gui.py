# -*- coding: utf-8 -*-
"""
disk_cleaner_gui.py — 磁盘清理工具 DiskCleaner (PySide6 全盘文件版)

- 扫描所选磁盘(C:/D:/…)的**全部文件与目录**, 按目录层级树展示(惰性分批加载)
- 顶部面包屑可跳级, 双击文件夹进入该层; 一键折叠全部(同 VSCode)
- 支持搜索 / 等级筛选 / 仅已勾选 / 全选开关; 勾选目录自动包含其子孙
- 类别按等级划分: 绿色(安全可删) / 橙色(谨慎删除) / 红色(不可删除);
  「可疑」= 规则未命中的未知文件(标红, 可单独筛选/交给 AI)
- 结果按磁盘分标签页(如 C 盘 / D 盘); 可勾选单一磁盘扫描清理
- AI 助手(自定义第三方 OpenAI 兼容 API): 内联判断, "AI判断"列显示
  安全可删 / 谨慎删除 / 不可删除, 判断中显示转圈; 「选中建议项」一键勾选
- AI 判断范围可选: 可删候选(绿+橙) / 不敢删项(红+橙+可疑) / 仅红 / 仅橙 /
  仅可疑 / 全部等级 / 仅当前盘; 红色与可疑项不会被自动勾选
- 删除红线: 磁盘根/Windows/Program Files/Users 根等关键位置禁止删除;
  非绿色项清理前二次确认

用法:
    pythonw disk_cleaner_gui.py              # 启动界面
    python  disk_cleaner_gui.py --selftest   # 无窗口只读自检
"""
import os
import sys
import re
import json
import time
import threading
import stat as stat_mod
import shutil
import subprocess
import ctypes
from functools import lru_cache
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSortFilterProxyModel, QRegularExpression,
    QAbstractTableModel, QAbstractItemModel, QModelIndex, QPoint, QSize, QRectF,
)
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit,
    QMessageBox, QFileDialog, QGraphicsDropShadowEffect, QTabWidget, QStackedWidget,
    QDialog, QLineEdit, QFormLayout, QTableView, QTreeView, QCheckBox, QSizePolicy,
    QComboBox, QSpinBox, QToolButton, QMenu,
)

# ==================== 基础工具 ====================
# 运行时数据目录: 源码运行 = 脚本所在目录; PyInstaller 打包后 = exe 所在目录。
# 配置/日志必须落在用户看得见、可写的位置, 绝不能写进 _internal / _MEIPASS。
APP_DATA_DIR = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, 'frozen', False)
                else os.path.dirname(os.path.abspath(__file__)))


def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '—'
    if n >= 2 ** 30:
        return f'{n / 2 ** 30:.2f} GB'
    if n >= 2 ** 20:
        return f'{n / 2 ** 20:.1f} MB'
    if n >= 2 ** 10:
        return f'{n / 2 ** 10:.0f} KB'
    return f'{int(n)} B'


def list_fixed_drives():
    """枚举本机固定磁盘, 返回 [{'Letter','FreeGB','TotalGB','UsedGB'}]。"""
    drives = []
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        root = letter + ':\\'
        if not os.path.isdir(root):
            continue
        try:
            dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
        except OSError:
            dtype = 3
        if dtype != 3:  # 只要 DRIVE_FIXED
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append({'Letter': letter,
                       'FreeGB': round(u.free / 2 ** 30, 2),
                       'TotalGB': round(u.total / 2 ** 30, 1),
                       'UsedGB': round((u.total - u.free) / 2 ** 30, 2)})
    return drives


def drives_free() -> dict:
    return {d['Letter']: d['FreeGB'] for d in list_fixed_drives()}


def run_ps(command: str) -> str:
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                           capture_output=True, timeout=60)
        return r.stdout.decode('gbk', errors='ignore').strip()
    except Exception:  # noqa: BLE001
        return ''


_RB_CACHE = {}


def recycle_bin_info(drive_root: str, timeout: float = 1.5):
    """返回 (MB, 个数)。

    SHQueryRecycleBinW 在部分卷(本机 D:)会无限挂起, 直接同步调用会把扫描线程
    卡死在第一行, 一条数据都发不出来且无法中断。这里放到 daemon 线程带超时查询,
    超时/失败返回 (0,0); 结果按盘缓存, 避免每次刷新都再挂一个线程。
    """
    key = drive_root.upper().rstrip('\\/')
    if key in _RB_CACHE:
        return _RB_CACHE[key]

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_ulonglong),
                    ('i64Size', ctypes.c_ulonglong),
                    ('i64NumItems', ctypes.c_ulonglong)]

    out = [0, 0]

    def query():
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        try:
            ok = ctypes.windll.shell32.SHQueryRecycleBinW(
                drive_root, ctypes.byref(info))
        except OSError:
            return
        if ok == 0:
            out[0] = round(info.i64Size / 1048576)
            out[1] = int(info.i64NumItems)

    t = threading.Thread(target=query, daemon=True, name='rb-query-' + key)
    t.start()
    t.join(timeout)
    _RB_CACHE[key] = (out[0], out[1])
    return _RB_CACHE[key]


def invalidate_recycle_cache():
    """清理完成后调用, 让下次扫描重新读取回收站大小。"""
    _RB_CACHE.clear()


# ==================== 分级规则: 绿色 / 橙色 / 红色 ====================
# rule 索引 → (level, note); level: G=绿色 橙=O 红=R

RULES = [
    ('G', '可再生缓存,安全删除'),          # 0
    ('G', '临时文件,可再生'),              # 1
    ('G', '日志/转储,可清理'),             # 2
    ('O', '依赖目录,删除后需重装/重建'),     # 3
    ('O', '包/构建缓存,删除后重新下载'),     # 4
    ('O', '软件安装目录,建议用卸载程序'),    # 5
    ('O', '应用数据,删除后软件需重新配置'),  # 6
    ('O', '安装包/镜像,确认后可删'),        # 7
    ('O', 'AVD 镜像,删除后需重新下载'),     # 8
    ('R', '个人文件,请勿删除'),            # 9
    ('R', '系统文件,请勿删除'),            # 10
    ('R', '版本库/重要数据,请勿删除'),      # 11
    ('R', '未知文件,请谨慎处理'),          # 12
    ('O', '回收站,清空不可恢复'),           # 13
]

LEVEL_NAME = {'G': '绿色', 'O': '橙色', 'R': '红色'}
LEVEL_COLOR = {  # (背景, 前景)
    'G': ('#E3F5EC', '#0E8A6B'),
    'O': ('#FDEEE3', '#C97A2B'),
    'R': ('#FBE7E7', '#C0392B'),
}

# ==================== AI 判断范围 ====================
# 规则 12「未知文件,请谨慎处理」= 未命中任何已知模式的文件, 本地标红但单列为「可疑」。
SUSPICIOUS_RULE = 12


def is_suspicious(rule: int) -> bool:
    """可疑项: 未命中任何已知规则的未知文件。"""
    return rule == SUSPICIOUS_RULE


# code -> (下拉显示名, 预算框简称, 悬停说明)
AI_SCOPES = {
    'GO':   ('可删候选(绿色 + 橙色) · 推荐', '绿色+橙色可删候选',
             '默认。只问本来就能删的项, 最省 token。'),
    'OO':   ('仅存疑项(橙色 + 未知)', '仅存疑(橙/未知)',
             '橙色项与未归类文件。'),
    'RISK': ('不敢删项(红色 + 橙色 + 可疑)', '红色+橙色+可疑',
             '只问用户不敢删的三类: 红色(个人/系统/版本库)、橙色(谨慎删除)、可疑(未知文件); 不含绿色。'),
    'R':    ('仅红色(不含可疑)', '仅红色(不含可疑)',
             '个人 / 系统 / 版本库等明确的禁删项; 未知文件在列表里也标红, 但归到「可疑」单独一项。'),
    'O':    ('仅橙色(谨慎删除)', '仅橙色',
             '依赖 / 包缓存 / 软件安装 / 应用数据等。'),
    'SUS':  ('仅可疑(未知 / 未归类文件)', '仅可疑',
             '可疑 = 未命中任何已知规则的未知文件, 最值得让 AI 看的一类。'),
    'ALL':  ('全部等级(含红色禁删项, 最费)', '全部等级',
             '所有等级, token 消耗最高。'),
    'CUR':  ('仅当前磁盘', '仅当前磁盘',
             '当前标签页所在磁盘的全部等级。'),
}
AI_SCOPE_ORDER = ('GO', 'OO', 'RISK', 'R', 'O', 'SUS', 'ALL', 'CUR')


def norm_ai_scope(value, default='GO'):
    """配置落盘/读取时统一校验, 未知值退回默认, 避免旧配置把范围写坏。"""
    v = str(value or '').strip().upper()
    return v if v in AI_SCOPES else default


def scope_accepts(scope, level, rule) -> bool:
    """某行是否落在 AI 判断范围内。绿色只在 GO/ALL/CUR 里出现。"""
    sus = is_suspicious(rule)
    if scope == 'RISK':
        return level in ('O', 'R') or sus
    if scope == 'R':
        return level == 'R' and not sus
    if scope == 'O':
        return level == 'O'
    if scope == 'SUS':
        return sus
    if scope == 'OO':
        return level == 'O' or sus
    if scope in ('ALL', 'CUR'):
        return True
    return level in ('G', 'O')       # GO 或未知值


# ==================== 全选范围 ====================
SELECT_ALL_LEVELS = ('G', 'O', 'R')
SELECT_LEVEL_HINT = {
    'G': '安全可删(缓存/临时)',
    'O': '谨慎删除(依赖/软件)',
    'R': '请勿删除(个人/系统/未知)',
}
UI_PREFS_FILE = os.path.join(APP_DATA_DIR, 'ui_prefs.json')


def norm_select_levels(value):
    """把任意来源的全选范围规整成 ('G','O','R') 固定顺序的元组, 至少保留一项。"""
    if isinstance(value, str):
        value = list(value)
    picked = {v for v in (value or ()) if v in SELECT_ALL_LEVELS}
    return tuple(v for v in SELECT_ALL_LEVELS if v in picked) or SELECT_ALL_LEVELS


def load_ui_prefs() -> dict:
    try:
        with open(UI_PREFS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_prefs(data: dict):
    try:
        with open(UI_PREFS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

# 模式按顺序匹配(先命中先生效), 传入路径已小写化
_PATTERNS = []


def _add(rule, *subs):
    for s in subs:
        _PATTERNS.append((s, rule))


_add(3, '\\node_modules\\', '\\bower_components\\', '\\site-packages\\', '\\.venv\\', '\\venv\\')
_add(8, '\\.android\\avd\\', '.avd\\')
_add(4, '\\.gradle\\', '\\.m2\\', '\\.nuget\\', '\\.cargo\\', '\\.ivy2\\', '\\.sbt\\',
     '\\go\\pkg\\mod\\', '\\.pub\\cache', '\\.conan\\')
_add(0, '\\__pycache__\\', '\\.mypy_cache\\', '\\.pytest_cache\\', '\\.ruff_cache\\',
     '\\.tox\\', '\\.nox\\', '\\.cache\\', '\\cache\\', '\\caches\\', '\\inetcache\\',
     '\\d3dscache\\', '\\dxcache\\', '\\glcache\\', '\\shadercache\\', '\\code cache\\',
     '\\gpucache\\', '\\cache_data\\', '\\cachestorage\\', '\\serviceworker\\',
     '\\filecache\\', '\\wheels\\', '\\_cacache\\', '\\npm-cache\\', '\\yarn\\cache',
     '\\pnpm-store\\', '\\go-build\\', '\\fontcache\\', '\\deliveryoptimization\\',
     'thumbcache_', 'iconcache_', '\\.vs\\')
_add(2, '\\crashdumps\\', '\\minidump\\', '\\livekernelreports\\', '\\wer\\',
     '\\errorreports\\', '\\crashreports\\', '\\windows\\logs\\',
     '\\softwaredistribution\\download', 'memory.dmp')
_add(1, '\\temp\\', '\\tmp\\', '\\temporary internet files\\', '\\recent\\')
_add(11, '\\.git\\', '\\.svn\\', '\\.hg\\')
_add(6, '\\appdata\\', '\\.vscode\\', '\\.idea\\')
_add(5, '\\program files\\', '\\program files (x86)\\', '\\programdata\\', '\\android\\sdk\\')
_add(10, '\\windows\\', 'pagefile.sys', 'hiberfil.sys', 'swapfile.sys', '\\winsxs\\',
     '\\boot\\', '\\efi\\', '\\system volume information\\')
_add(9, '\\desktop\\', '\\documents\\', '\\downloads\\', '\\pictures\\', '\\videos\\',
     '\\music\\', '\\favorites\\', '\\onedrive\\', '\\contacts\\', '\\saved games\\',
     '\\searches\\')

INSTALLER_EXT = ('.msi', '.msix', '.appx', '.iso', '.img', '.esd')


def classify_rule(path: str) -> int:
    """返回 RULES 索引。传入完整路径(大小写不敏感)。"""
    pl = path.lower()
    # 目录行路径不带尾分隔符, 统一补 '\\' 以便 '\\xxx\\' 类模式也能命中目录本体
    plx = pl if pl.endswith('\\') else pl + '\\'
    for sub, rule in _PATTERNS:
        if sub in plx:
            return rule
    if pl.endswith(INSTALLER_EXT):
        return 7
    return 12


def rule_level(rule: int) -> str:
    return RULES[rule][0]


def rule_note(rule: int) -> str:
    return RULES[rule][1]


# ==================== 全盘遍历 ====================

SKIP_DIR_NAMES = {
    '$recycle.bin', 'system volume information', 'recovery', 'config.msi',
    '$winreagent', 'found.000', 'documents and settings',
}
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _is_reparse(entry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, 'st_file_attributes', 0) & REPARSE)
    except OSError:
        return True


def walk_path(root: str, progress=None, is_interrupted=None, sink=None,
              batch_size=8000, trace=None):
    """遍历 root, 返回条目列表 [(path, is_dir, size_bytes, mtime, rule, special)]。

    - 文件逐个列出; 目录列出聚合大小(含全部子项)
    - 跳过 junction/符号链接与系统保护目录
    - progress(文件数) 每 2 万个文件回调一次
    - sink(rows) 若提供, 文件条目分批流式回调(目录聚合条目仍随返回值给出)
    - trace(当前目录, 已发现文件数): 每进入一个目录/每 2000 个文件回调一次,
      调用方负责按时间节流后写日志, 用于显示"扫描到哪了"
    """
    entries = []          # sink 为空时本地累积
    file_buf = []
    dir_buf = []          # sink 模式下目录条目缓冲(分批送, 避免最后一次性插入几十万行)
    dir_entries = []      # sink 为空时本地累积的目录条目
    counter = {'n': 0}

    def push(row):
        if sink is None:
            entries.append(row)
        else:
            file_buf.append(row)
            if len(file_buf) >= batch_size:
                sink(file_buf[:])
                file_buf.clear()

    def push_dir(row):
        if sink is None:
            dir_entries.append(row)
        else:
            dir_buf.append(row)
            if len(dir_buf) >= batch_size:
                sink(dir_buf[:])
                dir_buf.clear()

    def rec(path):
        total = 0
        # 每层入口先检查中断, 让"取消/重扫"能立刻冒泡退出
        if is_interrupted and is_interrupted():
            raise InterruptedError
        if trace:
            trace(path, counter['n'])
        try:
            it = os.scandir(path)
        except OSError:
            return 0
        try:
            for e in it:
                if is_interrupted and is_interrupted():
                    raise InterruptedError
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() in SKIP_DIR_NAMES or _is_reparse(e):
                            continue
                        sz = rec(e.path)
                        try:
                            mt = e.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            mt = 0.0
                        push_dir((e.path, True, sz, mt, classify_rule(e.path), ''))
                        total += sz
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        push((e.path, False, st.st_size, st.st_mtime,
                              classify_rule(e.path), ''))
                        total += st.st_size
                        counter['n'] += 1
                        if progress and counter['n'] % 20000 == 0:
                            progress(counter['n'])
                        if trace and counter['n'] % 2000 == 0:
                            trace(path, counter['n'])
                except InterruptedError:
                    # InterruptedError 是 OSError 的子类, 必须单独放行,
                    # 否则会被下面 except OSError 吞掉, 中断要空转完整盘才生效
                    raise
                except OSError:
                    continue
        except InterruptedError:
            raise
        except OSError:
            return total          # 迭代中途目录消失/无权限, 跳过该层即可
        finally:
            try:
                it.close()
            except (AttributeError, OSError):
                pass
        return total

    rec(root)
    if sink is not None:
        if file_buf:
            sink(file_buf[:])
            file_buf.clear()
        return dir_buf          # 剩余未满一批的目录条目, 由调用方送出
    entries.extend(dir_entries)
    return entries


def walk_drive(letter: str, progress=None, is_interrupted=None, trace=None):
    """整盘遍历; 盘根回收站作为合成条目(特殊清理动作)放在最前。"""
    out = []
    mb, cnt = recycle_bin_info(letter + ':\\')
    if cnt > 0 or mb > 0:
        out.append((letter + ':\\$Recycle.Bin', True, mb * 1048576, 0.0, 13, 'recycle'))
    out.extend(walk_path(letter + ':\\', progress, is_interrupted, trace=trace))
    return out


# ==================== 清理动作(带安全红线) ====================

def _norm(p: str) -> str:
    return os.path.normpath(p).lower().rstrip('\\')


def blocked_roots() -> set:
    """禁止整体删除的关键位置。"""
    home = os.environ.get('USERPROFILE', '')
    windir = os.environ.get('SystemRoot', r'C:\Windows')
    roots = {
        'c:', 'd:', 'e:', 'f:', 'g:',
        _norm(windir), _norm(os.path.join(windir, 'WinSxS')),
        r'c:\program files', r'c:\program files (x86)', r'c:\programdata',
        r'c:\users', _norm(home),
    }
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    roots |= {_norm(pf), _norm(pf86)}
    return {r for r in roots if r}


def is_blocked(path: str) -> bool:
    n = _norm(path)
    if n in blocked_roots():
        return True
    if n.endswith(('.sys',)) and n.rfind('\\') == n.find('\\'):
        base = os.path.basename(n)
        if base in ('pagefile.sys', 'hiberfil.sys', 'swapfile.sys'):
            return True
    return False


def _on_rmtree_error(func, path, _exc):
    try:
        os.chmod(path, stat_mod.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_path(path: str, is_dir: bool) -> bool:
    """删除单个文件/目录; 被占用或无权限则跳过。返回是否成功。"""
    if is_blocked(path):
        return False
    try:
        if is_dir:
            if not os.path.isdir(path):
                return os.path.exists(path) is False
            shutil.rmtree(path, onerror=_on_rmtree_error)
        else:
            if not os.path.isfile(path):
                return os.path.exists(path) is False
            try:
                os.chmod(path, stat_mod.S_IWRITE)
            except OSError:
                pass
            os.remove(path)
    except OSError:
        return not os.path.exists(path)
    return not os.path.exists(path)


def dedupe_entries(entries):
    """勾选了目录则其内部文件不重复统计/删除。entries: [(path, is_dir, size, mtime, rule, special)]"""
    dirset = {_norm(e[0]) for e in entries if e[1]}
    out = []
    for e in entries:
        if e[1]:
            out.append(e)
            continue
        parent = _norm(os.path.dirname(e[0]))
        covered = False
        while parent:
            if parent in dirset:
                covered = True
                break
            nxt = _norm(os.path.dirname(parent))
            if nxt == parent:
                break
            parent = nxt
        if not covered:
            out.append(e)
    return out


# ==================== 视觉主题 ====================

C_BG = '#F2F4F8'
C_CARD = '#FFFFFF'
C_INK = '#111827'
C_SUB = '#6B7280'
C_LINE = '#E5E9F0'
C_PRIMARY = '#0E8A6B'
C_PRIMARY_HOVER = '#0B7358'
C_PRIMARY_PRESS = '#095E48'
C_PRIMARY_SOFT = '#E6F4EE'
C_OK = '#0F8A63'
C_WARN = '#D97706'
C_DANGER = '#DC2626'
C_LOG_BG = '#141C28'
C_LOG_INK = '#A8B6C8'

QSS = f"""
* {{ font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 9.5pt; color: {C_INK}; }}
#root {{ background: {C_BG}; border-radius: 14px; }}
#title {{ font-size: 12pt; font-weight: 700; color: {C_INK}; background: transparent; }}
#subtitle {{ font-size: 8.5pt; color: {C_SUB}; background: transparent; }}
QLabel#hint  {{ color: {C_SUB}; font-size: 8.5pt; background: transparent; }}
QLabel#logLabel {{ color: {C_SUB}; font-size: 8.5pt; font-weight: 700; background: transparent; }}

/* 状态胶囊 */
#pill {{
    background: {C_OK}; color: white; border-radius: 11px;
    font-size: 8.5pt; font-weight: 700; padding: 3px 12px;
}}

/* 卡片 */
#statCard {{
    background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 12px;
}}
#statLabel {{ color: {C_SUB}; font-size: 8.5pt; background: transparent; border: none; }}
#statValue {{ color: {C_INK}; font-size: 19pt; font-weight: 700; background: transparent; border: none; }}
#accent {{ background: {C_PRIMARY}; border-radius: 2px; }}

/* 磁盘磁贴: Windows 资源管理器「此电脑」原生样式(白底/直角/蓝灰容量条) */
#diskCard {{
    background: #FFFFFF; border: 1px solid #E5E9F0; border-radius: 12px;
}}
#diskCard[active="true"] {{
    background: #E5F1FB; border: 1px solid #0070CB; border-radius: 12px;
}}
#diskCard[active="false"]:hover {{ background: #F5F9FD; border-color: #CDE3F7; }}
#diskIndicator {{ background: #0070CB; border-radius: 2px; border: none; }}
#diskName {{ font-size: 10pt; font-weight: 400; color: #000000; background: transparent; border: none; }}
#diskCaption {{ font-size: 9pt; color: #000000; background: transparent; border: none; }}
#diskCheck {{ font-size: 8.5pt; color: #000000; background: transparent; spacing: 5px; }}
#diskCheck:hover {{ color: #0070CB; }}
#diskCheck::indicator {{
    width: 14px; height: 14px; border-radius: 0px;
    background: #FFFFFF; border: 1px solid #767676;
}}
#diskCheck::indicator:checked {{ background: #0070CB; border: 1px solid #0070CB; }}
#diskCheck::indicator:unchecked:hover {{ border-color: #0070CB; }}

/* 窗口按钮 */
#capBtn {{
    background: transparent; border: none; border-radius: 8px;
    color: {C_SUB}; font-size: 12pt; padding: 0px;
}}
#capBtn:hover {{ background: #E2E7EE; color: {C_INK}; }}
#capClose {{
    background: transparent; border: none; border-radius: 8px;
    color: {C_SUB}; font-size: 12pt; padding: 0px;
}}
#capClose:hover {{ background: {C_DANGER}; color: white; }}

/* 标签页 */
QTabWidget::pane {{ border: none; background: transparent; margin-top: 2px; }}
QTabBar::tab {{
    background: transparent; border: none; border-bottom: 2px solid transparent;
    color: {C_SUB}; font-size: 9.5pt; padding: 8px 18px 6px 18px; margin-right: 6px;
}}
QTabBar::tab:selected {{
    color: {C_INK}; font-weight: 700; border-bottom: 2.5px solid {C_PRIMARY};
    background: {C_CARD};
    border-top-left-radius: 10px; border-top-right-radius: 10px;
}}
QTabBar::tab:hover:!selected {{ color: {C_INK}; }}

/* 按钮 */
QPushButton {{
    border: 1px solid {C_LINE}; border-radius: 10px; background: {C_CARD};
    color: {C_INK}; font-size: 9.5pt; padding: 9px 14px;
}}
QPushButton:hover {{ background: #F3F6FA; border-color: #D4DCE6; }}
QPushButton:pressed {{ background: #E8EDF3; }}
QPushButton:disabled {{ background: #EEF1F5; color: #9CA3AF; border-color: #EEF1F5; }}
#btnPrimary {{
    background: {C_PRIMARY}; color: white; border: none; font-weight: 700; font-size: 10pt;
}}
#btnPrimary:hover {{ background: {C_PRIMARY_HOVER}; }}
#btnPrimary:pressed {{ background: {C_PRIMARY_PRESS}; }}
#btnPrimary:disabled {{ background: #B7CFC6; color: #F0F7F4; }}
#btnGhost {{
    background: transparent; border: none; color: {C_SUB};
}}
#btnGhost:hover {{ background: #E7EBF1; color: {C_INK}; }}

/* 表格/视图 */
QTableWidget, QTableView, QTreeView {{
    background: {C_CARD}; border: none; border-radius: 12px;
    gridline-color: transparent; selection-background-color: {C_PRIMARY_SOFT};
    selection-color: {C_INK}; outline: none;
}}
QTableWidget::item, QTableView::item, QTreeView::item {{ padding: 5px 8px; border: none; }}
QTableWidget::item:hover, QTableView::item:hover, QTreeView::item:hover {{ background: #F5FAF8; }}
/* 不要给 ::branch 设 background: 一旦自定义该子控件, Qt 默认的展开三角会被抹掉 */
QHeaderView::section {{
    background: {C_CARD}; border: none; border-bottom: 1px solid {C_LINE};
    color: {C_SUB}; font-size: 8.5pt; font-weight: 700; padding: 8px 10px;
}}
QTableCornerButton::section {{ background: {C_CARD}; border: none; }}

/* 复选框 */
QCheckBox {{ background: transparent; spacing: 6px; }}
QCheckBox::indicator, QTableWidget::indicator, QTableView::indicator,
QTreeView::indicator {{
    width: 17px; height: 17px; border-radius: 5px;
}}
QCheckBox::indicator:unchecked, QTableWidget::indicator:unchecked,
QTableView::indicator:unchecked, QTreeView::indicator:unchecked {{
    background: white; border: 1.5px solid #C6D0DC;
}}
QCheckBox::indicator:unchecked:hover, QTableWidget::indicator:unchecked:hover,
QTableView::indicator:unchecked:hover, QTreeView::indicator:unchecked:hover {{ border-color: {C_PRIMARY}; }}
QCheckBox::indicator:checked, QTableWidget::indicator:checked,
QTableView::indicator:checked, QTreeView::indicator:checked {{
    background: {C_PRIMARY}; border: 1.5px solid {C_PRIMARY};
}}
QTreeView::indicator:indeterminate {{ background: #B7CFC6; border: 1.5px solid #B7CFC6; }}

/* 搜索框 / 表单控件 */
#searchBox {{
    background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 10px;
    padding: 8px 12px; font-size: 9pt;
}}
#searchBox:focus {{ border-color: {C_PRIMARY}; }}
/* 文件树面包屑 / 筛选工具栏 */
#crumbBar {{
    background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 10px;
}}
#crumbBtn {{
    background: transparent; border: none; border-radius: 6px;
    color: {C_SUB}; font-size: 9pt; padding: 4px 8px;
}}
#crumbBtn:hover {{ background: {C_PRIMARY_SOFT}; color: {C_PRIMARY}; }}
#crumbBtn[last="true"] {{ color: {C_INK}; font-weight: 700; }}
#crumbSep {{ color: #B9C2CE; font-size: 9pt; background: transparent; border: none; }}
#toolBar {{ background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 10px; }}
#filterCombo {{ padding: 6px 9px; }}
#miniBtn {{ padding: 6px 10px; font-size: 8.5pt; border-radius: 8px; }}
#toolCheck {{ font-size: 8.5pt; color: {C_SUB}; background: transparent; spacing: 5px; }}
#toolCheck:hover {{ color: {C_INK}; }}
#btnSelectScope {{
    background: transparent; border: 1px solid transparent;
    border-radius: 7px; padding: 2px;
}}
#btnSelectScope:hover {{ background: {C_PRIMARY_SOFT}; border-color: {C_LINE}; }}
#btnSelectScope[restricted="true"] {{
    background: {C_PRIMARY_SOFT}; border-color: {C_PRIMARY};
}}
QMenu {{
    background: {C_CARD}; border: 1px solid {C_LINE};
    border-radius: 8px; padding: 6px;
}}
QMenu::item {{
    padding: 6px 24px 6px 10px; border-radius: 5px;
    color: {C_INK}; font-size: 9pt;
}}
QMenu::item:selected {{ background: {C_PRIMARY_SOFT}; }}
QMenu::item:disabled {{ color: {C_SUB}; }}
QMenu::separator {{ height: 1px; background: {C_LINE}; margin: 4px 6px; }}
QLineEdit, QComboBox, QSpinBox {{
    background: {C_CARD}; border: 1px solid {C_LINE}; border-radius: 8px;
    padding: 6px 9px; font-size: 9pt; color: {C_INK};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {C_PRIMARY}; }}
QSpinBox {{ padding-right: 10px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0px; height: 0px; border: none; background: transparent;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {C_CARD}; border: 1px solid {C_LINE};
    selection-background-color: {C_PRIMARY_SOFT}; selection-color: {C_INK};
}}
QLabel#sectionHint {{
    color: {C_PRIMARY}; font-size: 8.5pt; font-weight: 700;
    background: {C_PRIMARY_SOFT}; border-radius: 8px; padding: 7px 10px;
}}

/* 日志 */
#logBox {{
    background: {C_LOG_BG}; color: {C_LOG_INK}; border-radius: 12px;
    border: none; font-family: 'Consolas', 'Cascadia Mono', monospace; font-size: 9pt;
    padding: 10px;
}}

/* 滚动条 */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #C7D0DA; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #A7B4C2; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #C7D0DA; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; }}
"""


# ==================== 标题栏(可拖动) ====================

class TitleBar(QFrame):
    def __init__(self, window):
        super().__init__()
        self.win = window
        self._drag = None
        self.setFixedHeight(46)
        self.setStyleSheet('background: transparent;')

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 2, 0)
        lay.setSpacing(10)
        icon = QFrame()
        icon.setFixedSize(24, 24)
        icon.setStyleSheet(f'background: {C_PRIMARY}; border-radius: 7px;')
        lay.addWidget(icon)
        tbox = QVBoxLayout()
        tbox.setSpacing(0)
        title = QLabel('磁盘清理工具')
        title.setObjectName('title')
        sub = QLabel('DiskCleaner  ·  全盘文件浏览  ·  AI 辅助判断')
        sub.setObjectName('subtitle')
        tbox.addWidget(title)
        tbox.addWidget(sub)
        lay.addLayout(tbox)
        lay.addStretch(1)

        self.pill = QLabel('就绪')
        self.pill.setObjectName('pill')
        self.pill.setAlignment(Qt.AlignCenter)
        self.pill.setFixedHeight(24)
        lay.addWidget(self.pill)
        lay.addSpacing(8)

        btn_min = QPushButton('—')
        btn_min.setObjectName('capBtn')
        btn_min.setFixedSize(40, 30)
        btn_min.clicked.connect(window.showMinimized)
        btn_close = QPushButton('×')
        btn_close.setObjectName('capClose')
        btn_close.setFixedSize(40, 30)
        btn_close.clicked.connect(window.close)
        lay.addWidget(btn_min)
        lay.addWidget(btn_close)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.win.frameGeometry().topLeft()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            self.win.move(ev.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag = None
        super().mouseReleaseEvent(ev)


# ==================== 磁盘磁贴(资源管理器「此电脑」原生风格) ====================

# 取自 Windows 资源管理器磁盘磁贴的实际像素值
C_WIN_BLUE = '#0070CB'      # 已用容量
C_WIN_TRACK = '#E6E6E6'     # 剩余容量
C_WIN_BORDER = '#BCBCBC'    # 容量条 1px 边框
C_WIN_LOW = '#C42B1C'       # 空间告急


def fmt_gb(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return '—'
    return str(int(f)) if abs(f - round(f)) < 0.005 else f'{f:.2f}'


class UsageBar(QWidget):
    """资源管理器容量条: 直角、1px 灰框、蓝色已用段 + 浅灰剩余段, 无圆角无渐变。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 0.0
        self._color = C_WIN_BLUE
        self.setFixedHeight(14)

    def set_usage(self, used_ratio: float, color: str):
        self._ratio = max(0.0, min(1.0, used_ratio))
        self._color = color
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C_WIN_TRACK))
        used = int((w - 2) * self._ratio)
        if used > 0:
            p.fillRect(1, 1, used, h - 2, QColor(self._color))
        p.setPen(QPen(QColor(C_WIN_BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class NativeDriveIcon(QWidget):
    """资源管理器硬盘图标: 深色盘体 + 绿色指示灯; 系统盘左上角叠加 Windows 四格徽标。

    如需改用当前操作系统自带的磁盘图标, 把 USE_SYSTEM_ICON 置 True 即可
    (走 QFileIconProvider / SHGetFileInfo, 拿到的是系统真实图标)。
    """

    USE_SYSTEM_ICON = False

    def __init__(self, letter: str, parent=None):
        super().__init__(parent)
        # 宽度固定, 高度随卡片行拉伸: 徽标对齐标题行, 盘体对齐容量条
        self.setFixedWidth(48)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.letter = letter
        self.system_drive = letter.upper() == os.environ.get(
            'SystemDrive', 'C:').rstrip(':\\').upper()
        self._sys_icon = None
        if self.USE_SYSTEM_ICON:
            try:
                from PySide6.QtCore import QFileInfo
                from PySide6.QtWidgets import QFileIconProvider
                self._sys_icon = QFileIconProvider().icon(
                    QFileInfo(letter + ':\\')).pixmap(32, 32)
            except Exception:  # noqa: BLE001
                self._sys_icon = None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._sys_icon is not None:
            p.drawPixmap(8, 0, self._sys_icon)
            p.end()
            return
        if self.system_drive:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C_WIN_BLUE))
            for r in range(2):
                for c in range(2):
                    p.drawRect(1 + c * 8, 1 + r * 8, 6, 6)
        top = 23  # 与右侧容量条同一行
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#EDEDED'))          # 盘体上沿的浅色斜面
        p.drawRoundedRect(9, top - 3, 30, 5, 2, 2)
        p.setBrush(QColor('#6D6D6D'))          # 外壳
        p.drawRoundedRect(1, top, 44, 14, 3, 3)
        p.setBrush(QColor('#313131'))          # 盘体
        p.drawRoundedRect(2, top + 1, 42, 12, 2, 2)
        p.setBrush(QColor('#565656'))          # 顶部高光
        p.drawRoundedRect(3, top + 2, 40, 3, 1, 1)
        p.setBrush(QColor('#46E06B'))          # 绿色指示灯
        p.drawEllipse(7, top + 7, 3, 3)
        p.end()


class DiskBarCard(QFrame):
    """磁盘磁贴 = 标签页: 资源管理器原生外观, 点击切换表格, 右上角勾选是否扫描。"""
    clicked = Signal(str)

    def __init__(self, letter: str, parent=None):
        super().__init__(parent)
        self.letter = letter
        self.setObjectName('diskCard')
        self.setFixedHeight(92)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty('active', 'false')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)

        # 左侧: 硬盘图标(系统盘带 Windows 四格徽标)
        self.drive_icon = NativeDriveIcon(letter)
        lay.addWidget(self.drive_icon, 0)

        col = QVBoxLayout()
        col.setSpacing(3)
        head_w = QWidget()
        head_w.setFixedHeight(20)
        head = QHBoxLayout(head_w)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.name = QLabel(f'本地磁盘 ({letter}:)')
        self.name.setObjectName('diskName')
        head.addWidget(self.name)
        head.addStretch(1)

        self.check = QCheckBox('扫描')
        self.check.setObjectName('diskCheck')
        self.check.setChecked(True)
        self.check.setCursor(Qt.ArrowCursor)
        self.check.setToolTip('勾选后该盘纳入扫描范围; 点「刷新扫描」可重新扫描')
        head.addWidget(self.check)
        col.addWidget(head_w)

        self.bar = UsageBar()
        col.addWidget(self.bar)

        self.caption = QLabel('检测中…')
        self.caption.setObjectName('diskCaption')
        self.caption.setFixedHeight(18)
        self.caption.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        col.addWidget(self.caption)
        col.addStretch(1)
        lay.addLayout(col, 1)

        # 标签页选中指示条(绝对定位在卡片底部)
        self.indicator = QFrame(self)
        self.indicator.setObjectName('diskIndicator')
        self.indicator.setFixedHeight(3)
        self.indicator.setVisible(False)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._place_indicator()

    def _place_indicator(self):
        self.indicator.setGeometry(18, self.height() - 11, self.width() - 36, 3)

    def mousePressEvent(self, ev):
        # 点在复选框上时不切换标签(正常情况事件由子控件自己消费)
        if not self.check.geometry().contains(ev.pos()):
            self.clicked.emit(self.letter)
        super().mousePressEvent(ev)

    def set_active(self, on: bool):
        self.setProperty('active', 'true' if on else 'false')
        self.style().unpolish(self)
        self.style().polish(self)
        self._place_indicator()
        self.indicator.setVisible(on)

    def update_disk(self, free_gb_v, total_gb_v):
        total = float(total_gb_v or 0)
        free = float(free_gb_v or 0)
        used = max(total - free, 0.0)
        ratio = (used / total) if total > 0 else 0.0
        low = total > 0 and (free / total < 0.10 or free < 15)
        color = C_WIN_LOW if low else C_WIN_BLUE
        self.bar.set_usage(ratio, color)
        self.caption.setText(f'{fmt_gb(free)} GB 可用，共 {fmt_gb(total)} GB')
        tip = '空间告急(低于10%或15GB)' if low else f'已用 {ratio * 100:.0f}%'
        self.caption.setToolTip(tip)
        self.name.setToolTip(tip)


# ==================== 文件明细对话框 ====================

class FileDetailDialog(QDialog):
    """列出某个条目(目录)内包含的文件(按大小降序), 只读展示, 不在此删除。"""

    def __init__(self, parent, name, path, is_file=False, is_thumb=False):
        super().__init__(parent)
        self.setWindowTitle(f'文件明细 — {name}')
        self.resize(760, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QLabel(f'路径: {path}')
        head.setObjectName('subtitle')
        head.setWordWrap(True)
        lay.addWidget(head)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(['大小', '修改时间', '文件'])
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.setColumnWidth(0, 100)
        table.setColumnWidth(1, 150)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(30)

        rows = []
        if is_file and os.path.isfile(path):
            try:
                st = os.stat(path)
                rows.append((st.st_size, path, datetime.fromtimestamp(st.st_mtime)))
            except OSError:
                pass
        elif is_thumb:
            try:
                for f in os.listdir(path):
                    if f.startswith('thumbcache_') or f.startswith('iconcache_'):
                        p = os.path.join(path, f)
                        try:
                            st = os.stat(p)
                            rows.append((st.st_size, p, datetime.fromtimestamp(st.st_mtime)))
                        except OSError:
                            pass
            except OSError:
                pass
        elif os.path.isdir(path):
            # 防御: 即便被旧入口误调用, 也绝不递归整棵大树把界面卡死
            try:
                for root, _dirs, files in os.walk(path):
                    for f in files:
                        p = os.path.join(root, f)
                        try:
                            st = os.stat(p)
                            rows.append((st.st_size, p, datetime.fromtimestamp(st.st_mtime)))
                        except OSError:
                            pass
                        if len(rows) >= 3000:
                            break
                    if len(rows) >= 3000:
                        break
            except OSError:
                pass

        rows.sort(key=lambda x: x[0], reverse=True)
        total = sum(r[0] for r in rows)
        table.setRowCount(min(len(rows), 500))
        for i, (sz, p, dt) in enumerate(rows[:500]):
            it0 = QTableWidgetItem(fmt_size(sz))
            it0.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
            table.setItem(i, 0, it0)
            table.setItem(i, 1, QTableWidgetItem(dt.strftime('%Y-%m-%d %H:%M')))
            table.setItem(i, 2, QTableWidgetItem(p))
        lay.addWidget(table, 1)

        foot = QLabel(f'共 {len(rows)} 个文件, 合计 {fmt_size(total)}'
                      + ('' if len(rows) <= 500 else '  (仅显示前 500 大文件)'))
        foot.setObjectName('hint')
        lay.addWidget(foot)

        btn = QPushButton('关闭')
        btn.setFixedHeight(36)
        btn.clicked.connect(self.accept)
        lay.addWidget(btn, 0, Qt.AlignRight)


# ==================== AI 助手(自定义第三方 API) ====================

AI_CONFIG_FILE = os.path.join(APP_DATA_DIR, 'ai_assistant.cfg')

VERDICT_SAFE = '安全可删'
VERDICT_CARE = '谨慎删除'
VERDICT_NO = '不可删除'
VERDICT_PEND = '判断中'
VERDICT_NONE = '—'
VERDICT_COLOR = {
    VERDICT_SAFE: '#0E8A6B',
    VERDICT_CARE: '#C97A2B',
    VERDICT_NO: '#C0392B',
    VERDICT_NONE: '#9CA3AF',
}

AI_CACHE_FILE = os.path.join(APP_DATA_DIR, 'ai_verdict_cache.json')
# 模式级判定: 归一化目录模式 -> (结论, 理由)。表格按行惰性查表, 无需回填 90 万行。
AI_GROUP_VERDICTS = {}

AI_SYSTEM_PROMPT = (
    '你是 Windows 磁盘清理顾问。输入每行描述一批同类文件: '
    '序号 | 目录模式 | 类型 | 数量 | 总大小 | 等级 | 样例名。'
    '请判断该模式下文件能否删除, 严格按行输出: 序号 | 结论 | 理由(15字内)。'
    '结论只能是: 安全可删 / 谨慎删除 / 不可删除。不要输出其它内容。'
    '口径: 缓存/临时/日志/构建产物=安全可删; 依赖目录/包缓存/应用配置=谨慎删除; '
    '系统组件/用户文档/项目源码/版本库=不可删除。'
    '等级与「可疑(未知)」只是本地规则的保守标注, 不是结论: 若该类模式实际是可再生成的'
    '缓存/临时/日志/旧版本残留, 仍应判安全可删; 只有确属系统组件/用户文档/项目源码/'
    '版本库才判不可删除。'
)


_HEXISH = frozenset('0123456789abcdef.-')


def _norm_seg(s):
    """哈希/GUID/版本号/纯数字 → '*'。先做廉价字符判断, 避免每行都跑正则。"""
    if s.isdigit():
        return '*'
    if len(s) >= 8 and all(ch in _HEXISH for ch in s):
        return '*'
    if len(s) >= 16:
        return re.sub(r'[0-9a-f]{16,}', '*', s)
    return s


# 语义关键词直接取自本地分级规则, 避免每个随机目录名都单独建组造成碎片。
# 实测: 按「目录尾3段」78 万行会碎成十几万个模式, 换成语义关键词后前 120 组可覆盖 ~90%。
_AI_KEYWORDS = frozenset(
    inner for sub, _r in _PATTERNS
    if sub.startswith('\\') and sub.endswith('\\')
    for inner in [sub.strip('\\')] if '\\' not in inner
) | frozenset({
    'cache', 'caches', 'temp', 'tmp', 'logs', 'log', 'build', 'dist',
    'node_modules', 'downloads', 'documents', 'desktop', 'src', 'bin',
    'obj', 'target', 'vendor', 'packages', 'backup', 'old', 'releases',
})


@lru_cache(maxsize=200000)
def ai_group_key(path: str, rule: int, is_dir: bool) -> str:
    """把一条文件/目录归一化成「语义模式」key, 同模式共用一个 AI 判定。

    key = 规则 | 文件/目录 | 最深命中的语义目录关键词 | 扩展名。
    不含盘符/用户名/随机目录名/版本号, 因此跨盘、跨项目、跨用户都能复用结论。
    """
    p = path.lower().replace('/', '\\')
    if len(p) > 2 and p[1] == ':':
        p = p[2:]
    parts = [s for s in p.split('\\') if s]
    kind = 'D' if is_dir else 'F'
    if not parts:
        return f'{rule}|{kind}|<root>|*'

    dirs = parts if is_dir else parts[:-1]
    keyword = '<misc>'
    for s in reversed(dirs):              # 只做集合查找, 不做全路径子串扫描
        s2 = _norm_seg(s)
        if s2 in _AI_KEYWORDS:
            keyword = s2
            break
    if is_dir:
        ext = '<dir>'
    else:
        ext = os.path.splitext(parts[-1])[1].lower()
        if not ext or len(ext) > 12:
            ext = '*'
    return f'{rule}|{kind}|{keyword}|{ext}'


def ai_group_brief(key: str) -> str:
    """把模式 key 还原成给模型看的紧凑目录描述。"""
    parts = key.split('|', 3)
    if len(parts) < 4:
        return key[:120]
    _rule_s, kind, kw, ext = parts
    typ = '目录' if kind == 'D' else f'文件 {ext}'
    tag = f'…\\{kw}\\…' if kw not in ('<misc>', '<root>') else '其它目录'
    return f'{tag} | {typ}'


def estimate_tokens(text: str) -> int:
    """粗估: 中文≈1字1token, 英文路径≈3~4字符1token, 混合取 3.0。"""
    return int(len(text) / 3.0) + 1


def build_ai_groups(models, scope='GO', max_groups=120,
                    current_letter=None, tick=None) -> list:
    """把全盘行压缩成待判断的模式组。

    scope 见 AI_SCOPES / scope_accepts(): GO / OO / RISK / R / O / SUS / ALL / CUR。
    返回按「数量+总大小」降序的前 max_groups 个组 dict。
    """
    scope = norm_ai_scope(scope)
    groups = {}
    seen = 0
    for letter, model in models.items():
        if scope == 'CUR' and letter != current_letter:
            continue
        for row in model.rows:
            seen += 1
            if tick and seen % 100000 == 0:
                tick()   # 90 万行归类时让界面保持响应
            path, is_dir, size, _mt, rule, special = row
            if special == 'recycle':
                continue
            level = rule_level(rule)
            if not scope_accepts(scope, level, rule):
                continue
            if level == 'R' and is_blocked(path):
                continue
            key = ai_group_key(path, rule, is_dir)
            g = groups.get(key)
            if g is None:
                g = {'key': key, 'count': 0, 'size': 0, 'rule': rule,
                     'is_dir': is_dir, 'level': level,
                     'sample': '\\'.join(path.split('\\')[-3:])[:56],
                     'letter': letter, 'path': path}
                groups[key] = g
            g['count'] += 1
            g['size'] += size
    ranked = sorted(groups.values(),
                    key=lambda g: g['count'] * 32768 + g['size'], reverse=True)
    return ranked[:max_groups]


def make_ai_batches(groups, max_chars=5200, per_batch=40) -> list:
    """按字符预算把模式组切成若干请求, 返回 [(prompt_text, [group_key,...])]。"""
    header = ('请判断以下「文件模式」能否删除(格式: 序号 | 目录模式 | 类型 | 数量 '
              '| 总大小 | 等级 | 样例名):\n')
    batches, lines, keys = [], [], []
    used = len(header)

    def flush():
        nonlocal used
        if lines:
            batches.append((header + '\n'.join(lines), keys[:]))
            lines.clear()
            keys.clear()
        used = len(header)

    for g in groups:
        lvl = ('可疑(未知)' if is_suspicious(g['rule'])
               else LEVEL_NAME.get(g['level'], '未知'))
        payload = (f'{ai_group_brief(g["key"])} | {g["count"]:,} 项 | '
                   f'{fmt_size(g["size"])} | {lvl} '
                   f'| 例 {g["sample"]}')
        if lines and (len(lines) >= per_batch or used + len(payload) + 8 > max_chars):
            flush()
        line = f'{len(lines) + 1} | {payload}'
        lines.append(line)       # 序号必须每批从 1 重新开始, 与解析结果对应
        keys.append(g['key'])
        used += len(line) + 1
    flush()
    return batches


def ai_cache_load(model: str):
    """只载入当前模型的缓存; 换模型自动失效但保留其它模型的记录。"""
    AI_GROUP_VERDICTS.clear()
    try:
        with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    prefix = model + '::'
    for k, v in raw.items():
        if k.startswith(prefix) and isinstance(v, list) and len(v) >= 2:
            AI_GROUP_VERDICTS[k[len(prefix):]] = (v[0], v[1])


def ai_cache_save(model: str, results: dict):
    """results: {group_key: (verdict, reason)}。追加写回, 不覆盖其它模型。"""
    if not results:
        return
    try:
        with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    for k, v in results.items():
        raw[model + '::' + k] = [v[0], v[1]]
    if len(raw) > 40000:
        raw = dict(list(raw.items())[-40000:])
    tmp = AI_CACHE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, AI_CACHE_FILE)
    except OSError:
        pass


def ai_cache_clear():
    AI_GROUP_VERDICTS.clear()
    try:
        os.remove(AI_CACHE_FILE)
    except OSError:
        pass


def dpapi_protect(data: bytes) -> bytes:
    """DPAPI 加密(绑定当前 Windows 用户), 失败时回退明文并加前缀标记。"""
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_byte))]

        in_buf = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        if ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(in_blob), u'DiskCleanerAI', None, None, None, 0, ctypes.byref(out_blob)):
            enc = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return b'DPAPI:' + enc
    except Exception:  # noqa: BLE001
        pass
    return b'PLAIN:' + data


def dpapi_unprotect(data: bytes) -> bytes:
    if data.startswith(b'PLAIN:'):
        return data[6:]
    if not data.startswith(b'DPAPI:'):
        return data
    raw = data[6:]
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_byte))]

        in_buf = ctypes.create_string_buffer(raw)
        in_blob = DATA_BLOB(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        # CryptUnprotectData 签名 7 参: (in, &descr, entropy, reserved, prompt, flags, out)
        if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return plain
    except Exception:  # noqa: BLE001
        pass
    return b''


class AiConfig:
    """API 配置: base_url / model / api_key(key 用 DPAPI 加密落盘, base64 编码防文本损坏)。"""

    def __init__(self):
        self.base_url = 'https://api.openai.com/v1'
        self.model = 'gpt-4o-mini'
        self.api_key = ''
        # Token 预算: 判断范围 / 最大模式组数 / 复用缓存
        self.scope = 'GO'
        self.max_groups = 120
        self.use_cache = True

    @staticmethod
    def load():
        import base64
        cfg = AiConfig()
        try:
            with open(AI_CONFIG_FILE, 'rb') as f:
                raw = f.read()
            if not raw.strip():
                return cfg
            for line in raw.decode('utf-8', errors='ignore').splitlines():
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k == 'base_url':
                    cfg.base_url = v.strip()
                elif k == 'model':
                    cfg.model = v.strip()
                elif k == 'api_key':
                    try:
                        blob = base64.b64decode(v.strip())
                    except Exception:  # noqa: BLE001
                        blob = v.encode('latin1', errors='ignore')
                    cfg.api_key = dpapi_unprotect(blob).decode('utf-8', errors='ignore')
                elif k == 'scope':
                    cfg.scope = norm_ai_scope(v)
                elif k == 'max_groups':
                    try:
                        cfg.max_groups = max(20, min(800, int(v.strip())))
                    except ValueError:
                        pass
                elif k == 'use_cache':
                    cfg.use_cache = v.strip() != '0'
        except OSError:
            pass
        return cfg

    def save(self):
        import base64
        enc_key = base64.b64encode(dpapi_protect(self.api_key.encode('utf-8'))).decode('ascii')
        content = (f'base_url={self.base_url}\nmodel={self.model}\napi_key={enc_key}\n'
                   f'scope={self.scope}\nmax_groups={self.max_groups}\n'
                   f'use_cache={1 if self.use_cache else 0}\n')
        with open(AI_CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(content)


class AiWorker(QThread):
    """分批把「文件模式」发给 OpenAI 兼容接口, 每批独立回填, 可随时中断。

    单批模式仍兼容旧的 done/failed 用法(配置对话框的「测试连接」)。
    """
    done = Signal(str)              # 每批回复文本(兼容单批调用方)
    batch_done = Signal(str, int)   # 回复文本, 批序号
    progress = Signal(int, int)     # 已完成批数, 总批数
    all_done = Signal(int)          # 成功批数
    failed = Signal(str)

    def __init__(self, cfg, prompt=None, batches=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.batches = list(batches) if batches else [prompt or '']

    def _post(self, prompt, use_max_tokens=True):
        import urllib.request
        import urllib.error
        url = self.cfg.base_url.rstrip('/') + '/chat/completions'
        payload = {
            'model': self.cfg.model,
            'messages': [
                {'role': 'system', 'content': AI_SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': 0.2,
        }
        if use_max_tokens:
            payload['max_tokens'] = 1400   # 限制输出, 防止模型自由发挥烧 token
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.cfg.api_key}',
        }, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content']
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='ignore')[:300]
            except Exception:  # noqa: BLE001
                pass
            # 部分兼容网关不认 max_tokens, 400 时去掉该字段重试一次
            if e.code == 400 and use_max_tokens and 'max_tokens' in detail:
                return self._post(prompt, use_max_tokens=False)
            raise RuntimeError(f'HTTP {e.code}: {detail}')

    def run(self):
        ok = 0
        for idx, prompt in enumerate(self.batches):
            if self.isInterruptionRequested():
                break
            try:
                text = self._post(prompt)
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e))
                return
            self.done.emit(text)
            self.batch_done.emit(text, idx)
            ok += 1
            self.progress.emit(ok, len(self.batches))
        self.all_done.emit(ok)


def parse_ai_reply(text: str):
    """解析 '序号 | 建议 | 理由' 行 → {idx(int): (verdict, reason)}。"""
    out = {}
    for line in (text or '').splitlines():
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 2:
            continue
        try:
            idx = int(''.join(ch for ch in parts[0] if ch.isdigit()))
        except ValueError:
            continue
        if idx <= 0:
            continue
        v = parts[1]
        if '安全' in v:
            verdict = VERDICT_SAFE
        elif '谨慎' in v or '慎' in v:
            verdict = VERDICT_CARE
        elif '不可' in v or '勿' in v:
            verdict = VERDICT_NO
        else:
            continue
        reason = parts[2][:60] if len(parts) > 2 else ''
        out[idx] = (verdict, reason)
    return out


class AiConfigDialog(QDialog):
    """第三方 API 配置对话框(OpenAI 兼容: base_url/model/api_key)。"""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle('AI 助手 — API 配置')
        self.setFixedWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        tip = QLabel('接入任意 OpenAI 兼容接口(官方 OpenAI / DeepSeek / Kimi / 智谱 / Ollama 等)。\n'
                     'API Key 使用 Windows DPAPI 加密保存在本机 ai_assistant.cfg, 不会上传。\n'
                     'AI 请求只发送目录模式/数量/大小/样例路径片段, 不发文件内容; '
                     '路径片段会发送给该服务商。')
        tip.setObjectName('hint')
        tip.setWordWrap(True)
        lay.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(10)
        self.ed_url = QLineEdit(cfg.base_url)
        self.ed_model = QLineEdit(cfg.model)
        self.ed_key = QLineEdit(cfg.api_key)
        self.ed_key.setEchoMode(QLineEdit.Password)
        form.addRow('接口地址 base_url', self.ed_url)
        form.addRow('模型 model', self.ed_model)
        form.addRow('API Key', self.ed_key)
        lay.addLayout(form)

        # ---- Token 预算 ----
        budget = QLabel('省 Token 设置 — 按「目录模式」分组判断, 一次结论自动套用到同组所有文件')
        budget.setObjectName('sectionHint')
        budget.setWordWrap(True)
        lay.addWidget(budget)

        self.cb_scope = QComboBox()
        for code in AI_SCOPE_ORDER:
            label, _short, tip = AI_SCOPES[code]
            self.cb_scope.addItem(label, code)
            self.cb_scope.setItemData(self.cb_scope.count() - 1, tip, Qt.ToolTipRole)
        idx = self.cb_scope.findData(norm_ai_scope(cfg.scope))
        self.cb_scope.setCurrentIndex(max(0, idx))

        scope_tip = QLabel('说明：「可疑」= 未命中任何已知规则的未知文件（列表中标红、'
                           '说明为「未知文件,请谨慎处理」）。红色与可疑项即使被判'
                           '「安全可删」也不会被「选中建议项」自动勾选。')
        scope_tip.setObjectName('hint')
        scope_tip.setWordWrap(True)

        self.sp_groups = QSpinBox()
        self.sp_groups.setRange(20, 800)
        self.sp_groups.setSingleStep(20)
        self.sp_groups.setValue(cfg.max_groups)
        self.sp_groups.setSuffix(' 组')

        self.ck_cache = QCheckBox('复用本机判定缓存(同模式不再重复请求)')
        self.ck_cache.setChecked(cfg.use_cache)

        bform = QFormLayout()
        bform.setSpacing(10)
        bform.addRow('判断范围', self.cb_scope)
        bform.addRow('最多模式组数', self.sp_groups)
        bform.addRow('', self.ck_cache)
        lay.addLayout(bform)
        lay.addWidget(scope_tip)

        row = QHBoxLayout()
        btn_clear = QPushButton('清除缓存')
        btn_clear.clicked.connect(self.on_clear_cache)
        btn_test = QPushButton('测试(发起真实请求)')
        btn_test.clicked.connect(self.on_test)
        btn_ok = QPushButton('保存')
        btn_ok.setObjectName('btnPrimary')
        btn_ok.clicked.connect(self.on_save)
        btn_cancel = QPushButton('取消')
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_clear)
        row.addStretch(1)
        row.addWidget(btn_test)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)

    def on_clear_cache(self):
        ai_cache_clear()
        QMessageBox.information(self, '已清除', '本机 AI 判定缓存已清空; 下次判断会重新请求全部模式。')

    def on_save(self):
        self.cfg.base_url = self.ed_url.text().strip() or self.cfg.base_url
        self.cfg.model = self.ed_model.text().strip() or self.cfg.model
        self.cfg.api_key = self.ed_key.text().strip()
        self.cfg.scope = norm_ai_scope(self.cb_scope.currentData())
        self.cfg.max_groups = self.sp_groups.value()
        self.cfg.use_cache = self.ck_cache.isChecked()
        try:
            self.cfg.save()
        except OSError as e:
            QMessageBox.warning(self, '保存失败', str(e))
            return
        ai_cache_load(self.cfg.model)
        self.accept()

    def on_test(self):
        self.cfg.base_url = self.ed_url.text().strip()
        self.cfg.model = self.ed_model.text().strip()
        self.cfg.api_key = self.ed_key.text().strip()
        if not self.cfg.api_key:
            QMessageBox.information(self, '提示', '请先填写 API Key。')
            return
        btn = self.sender()
        btn.setEnabled(False)
        btn.setText('测试中…')
        self._test_worker = AiWorker(self.cfg, '回复"OK"两个字母即可, 不要输出其它内容。')
        self._test_worker.done.connect(lambda t: self._on_test_done(btn, t, None))
        self._test_worker.failed.connect(lambda t: self._on_test_done(btn, None, t))
        self._test_worker.start()

    def _on_test_done(self, btn, text, err):
        btn.setEnabled(True)
        btn.setText('测试(发起真实请求)')
        if err:
            QMessageBox.warning(self, '连接失败', err)
        else:
            QMessageBox.information(self, '连接成功', f'模型回复: {(text or "").strip()[:120]}')


# ==================== 表格模型(全盘条目) ====================

SORT_ROLE = Qt.UserRole + 1
_QT_CHECKED = Qt.Checked.value


def want_checked(value):
    """兼容两种来源: 视图点击传来 int, 代码直接调用传来 CheckState 枚举。

    PySide6 里 `2 == Qt.Checked` 为 False(枚举不是 int), 必须显式取 .value,
    否则用户点击复选框会被当成「取消勾选」。
    """
    try:
        return int(getattr(value, 'value', value)) == _QT_CHECKED
    except (TypeError, ValueError):
        return value == Qt.Checked


# 行元组: (path, is_dir, size, mtime, rule, special)


def parent_path(path: str) -> str:
    """父目录(统一反斜杠); 盘根返回空串。"""
    p = path.rstrip('\\/')
    i = p.rfind('\\')
    if i < 0:
        i = p.rfind('/')
    if i < 0:
        return ''
    if i == 2 and len(p) > 2 and p[1] == ':':
        return p[:3]          # 'C:\\Users' -> 'C:\\'
    return p[:i] or ''


def row_data(row, c, role, store):
    """表格 / 文件树共用的单元格取值, 避免两份显示逻辑漂移。"""
    path, is_dir, size, mtime, rule, special = row
    level = rule_level(rule)
    if role == Qt.CheckStateRole and c == 0:
        if special == 'container':
            return None               # 导航容器不画复选框
        return Qt.Checked if store.is_checked(path) else Qt.Unchecked
    if role == SORT_ROLE:
        if c == 0:
            return os.path.basename(path.rstrip('\\')).lower()
        if c == 1:
            return {'R': 2, 'O': 1, 'G': 0}.get(level, 0)
        if c == 2:
            return size
        if c == 3:
            return rule_note(rule)
        return store.verdict_of(row)[0]
    if role == Qt.DisplayRole:
        if c == 0:
            name = os.path.basename(path.rstrip('\\')) or path
            return ('📁 ' if is_dir else '📄 ') + name
        if c == 1:
            return '目录' if special == 'container' else LEVEL_NAME.get(level, '红色')
        if c == 2:
            return fmt_size(size)
        if c == 3:
            return ('筛选结果所在目录(仅导航)' if special == 'container'
                    else rule_note(rule))
        if c == 4:
            return VERDICT_NONE if special == 'container' else store.verdict_of(row)[0]
    if role == Qt.ForegroundRole:
        if c == 1 and special == 'container':
            return QColor(C_SUB)
        if c == 1:
            return QColor(LEVEL_COLOR.get(level, ('', '#333'))[1])
        if c == 4:
            key = store.verdict_of(row)[0]
            return QColor(VERDICT_COLOR.get(key, '#9CA3AF'))
    if role == Qt.BackgroundRole and c == 1:
        if special == 'container':
            return QColor('#EEF1F5')
        return QColor(LEVEL_COLOR.get(level, ('#FFF', ''))[0])
    if role == Qt.TextAlignmentRole and c == 2:
        return int(Qt.AlignVCenter | Qt.AlignRight)
    if role == Qt.ToolTipRole:
        t = path
        if special == 'recycle':
            t += '\n(回收站, 使用清空动作)'
        else:
            try:
                t += '\n修改: ' + datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OSError, OverflowError):
                pass
        v = store.verdict_of(row)
        if v[1]:
            t += f'\nAI: {v[0]} — {v[1]}'
        return t
    return None


class TreeIndex:
    """全盘行的目录索引: parent -> 直接子项。按批增量构建, 不复制行元组。

    扫描是后序的(子文件先到, 目录聚合行后到), 因此遇到尚未出现的父目录时
    先放一个占位目录节点; 真实目录行到达后只更新 dirs[path], 树显示时按
    view_dirs 取最新行, 无需回头替换父列表里的引用(避免 90 万次线性查找)。
    """

    MAX_FILTER = 60000

    def __init__(self, letter: str):
        self.root = letter.upper() + ':\\'
        self.children = {}          # parent path -> [row, ...]
        self.dirs = {}              # dir path -> 最新目录行(占位或真实)
        self._seen = set()
        self.view_children = self.children
        self.view_dirs = self.dirs
        self.filter_count = 0
        self.truncated = False
        self.filter_ver = 0         # 每次筛选/排序变化 +1, 让节点缓存失效

    def placeholder(self, path):
        return (path, True, 0, 0.0, classify_rule(path), '')

    def add_batch(self, batch):
        for row in batch:
            self._add(row)

    def _add(self, row):
        path, is_dir = row[0], row[1]
        if is_dir:
            self.dirs[path] = row
        if path.rstrip('\\/').lower() == self.root.rstrip('\\/').lower():
            return
        if path in self._seen:
            return
        self._seen.add(path)
        par = parent_path(path) or self.root
        self.children.setdefault(par, []).append(row)
        guard = 0
        while par and par != self.root and par not in self.dirs and guard < 24:
            prow = self.placeholder(par)
            self.dirs[par] = prow
            pp = parent_path(par) or self.root
            if par not in self._seen:
                self.children.setdefault(pp, []).append(prow)
                self._seen.add(par)
            par = pp
            guard += 1

    def remove_paths(self, paths):
        """清理完成后同步移除索引里的条目(视图随后由主窗口刷新)。"""
        for p in paths:
            self.dirs.pop(p, None)
            self._seen.discard(p)
        for par, lst in list(self.children.items()):
            if any(r[0] in paths for r in lst):
                self.children[par] = [r for r in lst if r[0] not in paths]
        self.filter_ver += 1

    def build_filter(self, store, level='', checked_only=False, text=''):
        """按等级 / 已勾选 / 关键字生成「结果树」: 匹配项 + 其祖先目录链。

        level: '' / 'G' / 'O' / 'R' / 'S'(可疑=规则 12 未知文件)。

        无筛选时直接指向真实索引(零拷贝); 有筛选时一次全表扫描构建视图,
        最多 MAX_FILTER 条, 避免 200 万行把界面拖死。
        """
        active = bool(level or checked_only or text.strip())
        self.filter_ver += 1
        if not active:
            self.view_children, self.view_dirs = self.children, self.dirs
            self.filter_count, self.truncated = 0, False
            return
        low = text.strip().lower()
        matches = []
        for row in store.rows:
            if level == 'S':
                if not is_suspicious(row[4]):        # 可疑 = 规则 12 未知文件
                    continue
            elif level and rule_level(row[4]) != level:
                continue
            if low and low not in row[0].lower():
                continue
            if checked_only and store.checked_ancestor(row[0]) is None:
                continue
            matches.append(row)
            if len(matches) >= self.MAX_FILTER:
                self.truncated = True
                break
        row_items, dir_edges, sizes = {}, set(), {}
        root = self.root
        for row in matches:
            p = row[0]
            par = parent_path(p) or root
            row_items[(par, p)] = row
            cur, guard = par, 0
            while cur and cur != root and guard < 32:
                sizes[cur] = sizes.get(cur, 0) + row[2]
                dir_edges.add((parent_path(cur) or root, cur))
                cur, guard = parent_path(cur) or root, guard + 1
        vc, vd = {}, {}
        for (par, p), row in row_items.items():
            vc.setdefault(par, []).append(row)
            if row[1]:
                vd[p] = row
        matched = {p for _par, p in row_items}
        for pp, cur in dir_edges:
            if cur in matched:
                continue
            base = self.dirs.get(cur) or self.placeholder(cur)
            # 纯导航容器: 显示其下匹配项合计大小, 标成中性且不可勾选
            container = (cur, True, sizes.get(cur, 0), base[3], base[4], 'container')
            vd[cur] = container
            vc.setdefault(pp, []).append(container)
        self.view_children, self.view_dirs = vc, vd
        self.filter_count = len(matches)


class TreeNode:
    __slots__ = ('row', 'path', 'parent', 'is_dir', 'children', 'pos',
                 'rows_cache', 'cache_ver')

    def __init__(self, row, parent=None, pos=0):
        self.row = row
        self.path = row[0]
        self.parent = parent
        self.is_dir = bool(row[1])
        self.children = []
        self.pos = pos
        self.rows_cache = None
        self.cache_ver = -1


class FileTreeModel(QAbstractItemModel):
    """按目录层级惰性加载的树模型。

    - 只为「展开/可见」的节点创建 TreeNode, 每批 1500 行, 绝不一次灌 200 万行
    - 显示数据来自 TreeIndex.view_children(无筛选=真实层级, 有筛选=祖先链结果树)
    - 勾选支持继承: 父目录被勾, 后代显示已勾; 取消继承态会拆开父目录
    """
    HEADERS = ['名称', '类别', '大小', '说明', 'AI判断']
    FETCH = 1500
    checkedChanged = Signal()

    def __init__(self, store, tidx, parent=None):
        super().__init__(parent)
        self.store = store
        self.tidx = tidx
        self.root_path = tidx.root
        self.root_node = self._make_root(tidx.root)
        self.sort_col = 2
        self.sort_order = Qt.DescendingOrder

    @staticmethod
    def _make_root(path):
        return TreeNode((path, True, 0, 0.0, 0, ''))

    # —— QAbstractItemModel 结构 ——
    def node(self, idx):
        return idx.internalPointer() if idx.isValid() else self.root_node

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def _rows(self, node):
        if node.rows_cache is None or node.cache_ver != self.tidx.filter_ver:
            # 目录聚合行晚于子文件到达, children 里可能还是占位(size=0);
            # 这里统一换成 view_dirs 中的最新行, 排序/显示才用真实大小。
            vd = self.tidx.view_dirs
            rows = [vd.get(r[0], r)
                    for r in self.tidx.view_children.get(node.path, ())]
            col, rev = self.sort_col, self.sort_order == Qt.DescendingOrder
            if col == 0:
                key = lambda r: os.path.basename(r[0].rstrip('\\')).lower()
            elif col == 1:
                key = lambda r: 'GOR'.find(rule_level(r[4]))
            elif col == 3:
                key = lambda r: rule_note(r[4])
            elif col == 4:
                key = lambda r: self.store.verdict_of(r)[0]
            else:
                key = lambda r: r[2]
            try:
                rows.sort(key=key, reverse=rev)
                rows.sort(key=lambda r: 0 if r[1] else 1)   # 稳定排序: 文件夹永远在前
            except (TypeError, ValueError):
                pass
            node.rows_cache = rows
            node.cache_ver = self.tidx.filter_ver
        return node.rows_cache

    def rowCount(self, parent=QModelIndex()):
        node = self.node(parent)
        return len(node.children) if node.is_dir else 0

    def hasChildren(self, parent=QModelIndex()):
        node = self.node(parent)
        return node.is_dir and bool(self._rows(node))

    def canFetchMore(self, parent=QModelIndex()):
        node = self.node(parent)
        return node.is_dir and len(node.children) < len(self._rows(node))

    def fetchMore(self, parent=QModelIndex()):
        node = self.node(parent)
        rows = self._rows(node)
        n0 = len(node.children)
        n = min(self.FETCH, len(rows) - n0)
        if n <= 0:
            return
        self.beginInsertRows(parent, n0, n0 + n - 1)
        for i in range(n0, n0 + n):
            child = TreeNode(rows[i], node, i)
            node.children.append(child)
        self.endInsertRows()

    def index(self, row, col, parent=QModelIndex()):
        node = self.node(parent)
        if row < 0 or col < 0 or col >= len(self.HEADERS) or row >= len(node.children):
            return QModelIndex()
        return self.createIndex(row, col, node.children[row])

    def parent(self, idx):
        if not idx.isValid():
            return QModelIndex()
        p = self.node(idx).parent
        if p is None or p is self.root_node or p.parent is None:
            return QModelIndex()
        return self.createIndex(p.pos, 0, p)

    def flags(self, idx):
        if not idx.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if idx.column() == 0:
            node = self.node(idx)
            row = self.tidx.view_dirs.get(node.path, node.row)
            if row[5] != 'container':     # 筛选结果里的导航容器不可勾选
                f |= Qt.ItemIsUserCheckable
        return f

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        node = self.node(idx)
        row = self.tidx.view_dirs.get(node.path, node.row)
        if role == Qt.ToolTipRole:
            t = row_data(row, idx.column(), role, self.store) or ''
            anc = self.store.checked_ancestor(node.path)
            if anc and anc != node.path:
                t += f'\n(由上级目录整体勾选: {anc})'
            return t
        return row_data(row, idx.column(), role, self.store)

    def setData(self, idx, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or idx.column() != 0:
            return False
        node = self.node(idx)
        self.store.set_checked(node.path, want_checked(value))
        self._emit_subtree(node)
        self.checkedChanged.emit()
        return True

    def _emit_subtree(self, node):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.parent is not None:
                i = self.createIndex(n.pos, 0, n)
                self.dataChanged.emit(i, i, [Qt.CheckStateRole])
            stack.extend(n.children)

    # —— 视图操作 ——
    def set_root(self, path):
        self.beginResetModel()
        self.root_path = path
        self.root_node = self._make_root(path)
        self.endResetModel()
        self.fetchMore(QModelIndex())

    def refresh(self):
        self.beginResetModel()
        self.root_node = self._make_root(self.root_path)
        self.endResetModel()
        self.fetchMore(QModelIndex())

    def sort(self, col, order=Qt.AscendingOrder):
        self.sort_col, self.sort_order = col, order
        self.tidx.filter_ver += 1
        self.refresh()

    def expanded_paths(self, view):
        out, stack = [], list(self.root_node.children)
        while stack:
            n = stack.pop()
            if view.isExpanded(self.createIndex(n.pos, 0, n)):
                out.append(n.path)
            stack.extend(n.children)
        return out

    def restore_expanded(self, view, paths):
        want, stack = set(paths), list(self.root_node.children)
        while stack and want:
            n = stack.pop()
            if n.path in want:
                view.setExpanded(self.createIndex(n.pos, 0, n), True)
                want.discard(n.path)
            stack.extend(n.children)

    def loaded_nodes(self):
        out, stack = [], list(self.root_node.children)
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(n.children)
        return out

    def refresh_column(self, col=4):
        for n in self.loaded_nodes():
            i = self.createIndex(n.pos, col, n)
            self.dataChanged.emit(i, i)


class FileTableModel(QAbstractTableModel):
    HEADERS = ['名称', '类别', '大小', '说明', 'AI判断']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.checked = set()      # path
        self.verdicts = {}        # path -> (verdict, reason), 仅存「判断中」等逐行状态
        self._row_of = {}         # path -> row index
        self.tidx = None          # 绑定的 TreeIndex(勾选目录时回查占位目录行)

    def is_checked(self, path):
        """勾选含继承: 自身或任一祖先目录被勾选, 即视为已勾选。"""
        return self.checked_ancestor(path) is not None

    def checked_ancestor(self, path):
        p, guard = path, 0
        root = self.tidx.root.rstrip('\\/').lower() if self.tidx else ''
        while p and guard < 48:
            if root and p.rstrip('\\/').lower() == root:
                return None       # 盘根永不作为勾选项
            if p in self.checked:
                return p
            p = parent_path(p)
            guard += 1
        return None

    def set_checked(self, path, want):
        """统一勾选入口: 取消继承态时拆开父目录, 保留其余兄弟的勾选。"""
        if want:
            self.checked.add(path)
            return
        anc = self.checked_ancestor(path)
        if anc and anc != path and self.tidx is not None:
            sibs = (self.tidx.view_children.get(anc)
                    or self.tidx.children.get(anc) or ())
            for sib in sibs:
                if sib[0] != path:
                    self.checked.add(sib[0])
            self.checked.discard(anc)
        self.checked.discard(path)

    def row_by_path(self, path):
        i = self._row_of.get(path)
        if i is not None:
            return self.rows[i]
        if self.tidx is not None:
            return self.tidx.dirs.get(path)
        return None

    def verdict_of(self, row):
        """逐行状态优先, 否则按目录模式惰性查 AI 结论(覆盖全盘且无需回填 90 万行)。"""
        v = self.verdicts.get(row[0])
        if v:
            return v
        return AI_GROUP_VERDICTS.get(ai_group_key(row[0], row[4], row[1]),
                                    (VERDICT_NONE, ''))

    # —— 结构 ——
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if r >= len(self.rows):
            return None
        path, is_dir, size, mtime, rule, special = self.rows[r]
        level = rule_level(rule)

        if role == Qt.CheckStateRole and c == 0:
            return Qt.Checked if path in self.checked else Qt.Unchecked
        if role == SORT_ROLE:
            if c == 0:
                return path.lower()
            if c == 1:
                return {'R': 2, 'O': 1, 'G': 0}.get(level, 0)
            if c == 2:
                return size
            if c == 3:
                return rule_note(rule)
            return self.verdict_of(self.rows[r])[0]
        if role == Qt.DisplayRole:
            if c == 0:
                name = os.path.basename(path.rstrip('\\')) or path
                return ('📁 ' if is_dir else '📄 ') + name
            if c == 1:
                return LEVEL_NAME.get(level, '红色')
            if c == 2:
                return fmt_size(size)
            if c == 3:
                return rule_note(rule)
            if c == 4:
                return self.verdict_of(self.rows[r])[0]
        if role == Qt.ForegroundRole:
            if c == 1:
                return QColor(LEVEL_COLOR.get(level, ('', '#333'))[1])
            if c == 4:
                key = self.verdict_of(self.rows[r])[0]
                base = VERDICT_COLOR.get(key, '#9CA3AF')
                return QColor(base)
        if role == Qt.BackgroundRole and c == 1:
            return QColor(LEVEL_COLOR.get(level, ('#FFF', ''))[0])
        if role == Qt.TextAlignmentRole and c == 2:
            return int(Qt.AlignVCenter | Qt.AlignRight)
        if role == Qt.ToolTipRole:
            t = path
            if special == 'recycle':
                t += '\n(回收站, 使用清空动作)'
            else:
                try:
                    t += '\n修改: ' + datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                except (ValueError, OSError, OverflowError):
                    pass
            v = self.verdict_of(self.rows[r])
            if v[1]:
                t += f'\nAI: {v[0]} — {v[1]}'
            return t
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.column() == 0:
            path = self.rows[index.row()][0]
            if want_checked(value):
                self.checked.add(path)
            else:
                self.checked.discard(path)
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            return True
        return False

    # —— 数据操作 ——
    def append_rows(self, batch):
        """纯数据追加(不再挂在视图上)。

        旧实现每批 8000 行都 beginInsertRows, 代理会对累计 200 万行反复
        重建过滤映射, 界面直接「未响应」。现在扫描只维护索引, 由树模型
        按当前层级惰性显示。
        """
        if not batch:
            return
        for row in batch:
            self._row_of[row[0]] = len(self.rows)
            self.rows.append(row)
            if row[5] != 'recycle' and rule_level(row[4]) == 'G':
                self.checked.add(row[0])

    def sort_rows(self, column, order=Qt.AscendingOrder):
        """在源模型层用 Python 排序(比 QSortFilterProxyModel.sort 快一个数量级)。

        20 万行时代理排序需 20s+, 这里 <1s。排序后重建路径索引并整体刷新。
        """
        reverse = (order == Qt.DescendingOrder)
        if column == 0:
            key = lambda r: r[0].lower()
        elif column == 1:
            key = lambda r: 'GOR'.find(rule_level(r[4]))
        elif column == 2:
            key = lambda r: r[2]
        elif column == 3:
            key = lambda r: rule_note(r[4])
        elif column == 4:
            key = lambda r: self.verdict_of(r)[0]
        else:
            return
        self.beginResetModel()
        try:
            self.rows.sort(key=key, reverse=reverse)
        except (TypeError, ValueError):
            pass
        self._row_of = {r[0]: i for i, r in enumerate(self.rows)}
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self.checked.clear()
        self.verdicts.clear()
        self._row_of.clear()
        self.endResetModel()

    def remove_paths(self, paths):
        paths = set(paths)
        if not paths:
            return
        self.rows = [r for r in self.rows if r[0] not in paths]
        self.checked -= paths
        for p in paths:
            self.verdicts.pop(p, None)
        self._row_of = {r[0]: i for i, r in enumerate(self.rows)}
        if self.tidx is not None:
            self.tidx.remove_paths(paths)

    def set_verdict(self, path, verdict, reason=''):
        self.verdicts[path] = (verdict, reason)
        r = self._row_of.get(path)
        if r is not None:
            self.dataChanged.emit(self.index(r, 4), self.index(r, 4))

    def clear_verdict(self, path):
        """移除逐行占位状态, 让该行回落到模式级 AI 结论。"""
        self.verdicts.pop(path, None)
        r = self._row_of.get(path)
        if r is not None:
            self.dataChanged.emit(self.index(r, 4), self.index(r, 4))

    def checked_entries(self):
        """只遍历显式勾选项, 不再扫 200 万行全表。"""
        out = []
        for p in self.checked:
            r = self.row_by_path(p)
            if r is not None:
                out.append(r)
        return out

    def size_hint(self):
        return len(self.rows)


class FileFilterProxy(QSortFilterProxyModel):
    def sort(self, column, order=Qt.AscendingOrder):
        """接管排序: 交给源模型用 Python 原地排序, 避免代理逐行取 data 的巨慢路径。"""
        src = self.sourceModel()
        if src is None or not hasattr(src, 'sort_rows') or column < 0:
            return super().sort(column, order)
        src.sort_rows(column, order)  # beginResetModel 会自动传播到代理

    def filterAcceptsRow(self, src_row, src_parent):
        pat = self.filterRegularExpression()
        if not pat or not pat.pattern():
            return True
        m = self.sourceModel()
        if src_row >= len(m.rows):
            return True
        path = m.rows[src_row][0]
        return pat.match(path).hasMatch()


# ==================== 后台线程 ====================

class ScanWorker(QThread):
    """全盘遍历单个磁盘, 分批发射条目, 并按时间节流回传当前扫描目录。"""
    batch = Signal(str, object)        # letter, [row tuples]
    progress = Signal(str, int)        # letter, 文件数
    trace = Signal(str, str, int)      # letter, 当前目录, 已发现文件数
    done = Signal(str, int)            # letter, 总条目数
    failed = Signal(str, str)

    BATCH = 8000
    TRACE_INTERVAL = 0.35              # 日志最多每 0.35 秒一条, 避免刷屏

    def __init__(self, letter, parent=None):
        super().__init__(parent)
        self.letter = letter

    def run(self):
        try:
            total = 0
            last_trace = [0.0]
            # 先报告"线程已启动", 避免卡在回收站查询时日志一片空白
            self.trace.emit(self.letter, self.letter + ':\\', 0)

            def sink(batch):
                nonlocal total
                if self.isInterruptionRequested():
                    raise InterruptedError
                total += len(batch)
                self.batch.emit(self.letter, batch)

            def on_trace(path, n):
                now = time.monotonic()
                if now - last_trace[0] < self.TRACE_INTERVAL:
                    return
                last_trace[0] = now
                self.trace.emit(self.letter, path, n)

            # 先遍历文件: 数据要尽早进表格, 绝不能被下面的 Shell 查询挡住
            dir_rows = walk_path(self.letter + ':\\',
                                 progress=lambda n: self.progress.emit(self.letter, n),
                                 is_interrupted=self.isInterruptionRequested,
                                 sink=sink,
                                 trace=on_trace)
            if dir_rows:
                sink(dir_rows)   # 目录聚合条目剩余一批
            # 回收站查询放最后(带超时): SHQueryRecycleBinW 在部分卷上会挂死
            try:
                mb, cnt = recycle_bin_info(self.letter + ':\\', timeout=1.5)
            except Exception:  # noqa: BLE001
                mb, cnt = 0, 0
            if cnt > 0 or mb > 0:
                sink([(self.letter + ':\\$Recycle.Bin', True, mb * 1048576, 0.0, 13, 'recycle')])
            self.done.emit(self.letter, total)
        except InterruptedError:
            self.done.emit(self.letter, -1)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self.letter, str(e))


class CleanWorker(QThread):
    """删除勾选条目(文件/目录/回收站)。"""
    log = Signal(str)
    done = Signal(float, object, object, object)  # freed_mb, before_map, after_map, deleted_paths

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items  # [(path, is_dir, size, special)]

    def run(self):
        before = drives_free()
        freed = 0
        deleted = []
        for path, is_dir, size, _mtime, _rule, special in self.items:
            if self.isInterruptionRequested():
                break
            if special == 'recycle':
                letter = path[0].upper()
                run_ps(f'Clear-RecycleBin -DriveLetter {letter} -Force -ErrorAction SilentlyContinue')
                freed += size
                deleted.append(path)
                self.log.emit(f'已清空 {letter}: 盘回收站(约 {fmt_size(size)})')
                continue
            if is_blocked(path):
                self.log.emit(f'安全红线拦截, 跳过: {path}')
                continue
            ok = remove_path(path, is_dir)
            if ok:
                freed += size
                deleted.append(path)
                self.log.emit(f'已删除: {path}')
            else:
                self.log.emit(f'跳过(占用/无权限): {path}')
        self.msleep(1200)  # 等待文件系统释放句柄后再统计
        after = drives_free()
        self.done.emit(freed, before, after, deleted)


# ==================== 主窗口 ====================

class MainWindow(QWidget):
    AI_BATCH_CHARS = 5200     # 单批请求的用户消息字符上限
    AI_BATCH_GROUPS = 40      # 单批最多模式组数
    SPIN_FRAMES = '|/-\\'

    def __init__(self):
        super().__init__()
        self.setWindowTitle('磁盘清理工具 DiskCleaner')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(1280, 860)

        self.models = {}       # letter -> FileTableModel(数据仓库, 不直接挂视图)
        self.tree_indexes = {} # letter -> TreeIndex(目录索引)
        self.tree_models = {}  # letter -> FileTreeModel(惰性层级视图)
        self.views = {}        # letter -> QTreeView
        self.crumb_boxes = {}  # letter -> 面包屑布局
        self.crumb_ups = {}    # letter -> 返回上级按钮
        self.filter_level = {} # letter -> 等级下拉
        self.filter_checked = {}   # letter -> 仅已勾选
        self.chk_all = {}      # letter -> 全选开关
        self.select_scope_btns = {}   # letter -> 全选范围齿轮按钮
        self._scope_menu = None       # 非阻塞弹出菜单需保持引用
        self.select_all_levels = norm_select_levels(
            load_ui_prefs().get('select_all_levels'))  # 全局共享, 落盘
        self.count_labels = {}
        self.search_boxes = {}
        self.drive_checks = {}
        self.disk_cards = {}
        self.pages = {}            # letter -> 该盘的文件树页面
        self._tree_dirty = set()   # 扫描中待刷新的盘
        self.current_letter = None
        self.scan_workers = {}
        self._zombies = []         # 已隔离但尚未退出的旧扫描线程(防止 QThread 被提前析构)
        self.scan_trace = {}       # letter -> (当前目录, 已发现文件数), 供日志与状态栏显示进度
        self.clean_worker = None
        self.ai_worker = None
        self.ai_cfg = AiConfig.load()
        ai_cache_load(self.ai_cfg.model)
        self.ai_targets = []   # [(model, path)] 各模式代表行, 供「判断中」转圈
        self.ai_batches = []   # [(prompt, [group_key,...])]
        self.ai_reps_by_batch = []
        self.ai_results = {}   # 本轮新增的 pattern -> (verdict, reason)
        self.ai_progress = (0, 0)
        self._busy = None      # None/'scan'/'ai'/'clean'
        self._spin = 0
        self._last_sort = (2, Qt.DescendingOrder)

        self._init_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(140)
        self._timer.timeout.connect(self._spin_tick)
        self._timer.start()
        self._sum_timer = QTimer(self)      # 勾选统计防抖: 不再每次点击都扫全表
        self._sum_timer.setSingleShot(True)
        self._sum_timer.setInterval(320)
        self._sum_timer.timeout.connect(self._do_update_checked_sum)
        self._view_timer = QTimer(self)     # 扫描中节流刷新当前可见层级
        self._view_timer.setInterval(900)
        self._view_timer.timeout.connect(self._refresh_dirty_trees)
        self._view_timer.start()

        QTimer.singleShot(400, self.on_scan)

    # ---------- UI ----------
    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        self.root = QFrame()
        self.root.setObjectName('root')
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 60))
        self.root.setGraphicsEffect(shadow)
        outer.addWidget(self.root)

        lay = QVBoxLayout(self.root)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        self.titlebar = TitleBar(self)
        lay.addWidget(self.titlebar)

        body = QHBoxLayout()
        body.setContentsMargins(16, 0, 16, 0)
        body.setSpacing(14)
        main = QVBoxLayout()
        main.setSpacing(12)

        # 磁盘卡即标签页: 点击切换表格, 卡片右上角勾选是否扫描
        self.stats_row = QHBoxLayout()
        self.stats_row.setSpacing(12)
        drives = list_fixed_drives()
        for d in drives:
            card = DiskBarCard(d['Letter'])
            card.update_disk(d['FreeGB'], d['TotalGB'])
            card.clicked.connect(self.switch_disk)
            cb = card.check
            cb.stateChanged.connect(lambda _s, l=d['Letter']: self.on_drive_toggled(l))
            cb.stateChanged.connect(self.update_checked_sum)
            self.drive_checks[d['Letter']] = cb
            self.disk_cards[d['Letter']] = card
            self.stats_row.addWidget(card, 1)
        self.card_sum = QFrame()
        self.card_sum.setObjectName('statCard')
        self.card_sum.setFixedHeight(92)
        cl = QVBoxLayout(self.card_sum)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(2)
        lbl = QLabel('勾选合计')
        lbl.setObjectName('statLabel')
        self.sum_value = QLabel('0 B')
        self.sum_value.setObjectName('statValue')
        self.sum_sub = QLabel('共 0 项')
        self.sum_sub.setObjectName('statLabel')
        cl.addWidget(lbl)
        cl.addWidget(self.sum_value)
        cl.addWidget(self.sum_sub)
        self.stats_row.addWidget(self.card_sum, 1)
        main.addLayout(self.stats_row)

        tip_row = QHBoxLayout()
        tip_row.setContentsMargins(2, 0, 2, 0)
        tip_row.addStretch(1)
        tip = QLabel('点击磁盘卡片切换查看 · 卡片上勾选决定是否纳入扫描 · 「刷新扫描」重新扫描')
        tip.setObjectName('hint')
        tip_row.addWidget(tip)
        main.addLayout(tip_row)

        # 每盘一个页面, 由上方磁盘卡(标签页)切换
        self.stack = QStackedWidget()
        letters = [d['Letter'] for d in drives] or ['C']
        self._filter_timers = {}
        for letter in letters:
            tab = QWidget()
            v = QVBoxLayout(tab)
            v.setContentsMargins(0, 4, 0, 0)
            v.setSpacing(8)

            # —— 面包屑栏(点击任意一级跳到该目录) ——
            crumb = QFrame()
            crumb.setObjectName('crumbBar')
            crumb.setFixedHeight(38)
            cl = QHBoxLayout(crumb)
            cl.setContentsMargins(8, 2, 8, 2)
            cl.setSpacing(2)
            btn_up = QPushButton('↑')
            btn_up.setObjectName('miniBtn')
            btn_up.setFixedWidth(30)
            btn_up.setToolTip('返回上级目录')
            btn_up.clicked.connect(lambda _=False, l=letter: self.on_crumb_up(l))
            cl.addWidget(btn_up)
            sep0 = QLabel('›')
            sep0.setObjectName('crumbSep')
            sep0.setFixedWidth(14)
            sep0.setAlignment(Qt.AlignCenter)
            cl.addWidget(sep0)
            crumb_holder = QHBoxLayout()
            crumb_holder.setSpacing(2)
            cl.addLayout(crumb_holder, 1)
            btn_fold = QPushButton('折叠全部')
            btn_fold.setObjectName('miniBtn')
            btn_fold.setFixedWidth(88)
            btn_fold.setToolTip('折叠所有已展开的文件夹(同 VSCode)')
            btn_fold.clicked.connect(lambda _=False, l=letter: self.views[l].collapseAll())
            cl.addWidget(btn_fold)
            v.addWidget(crumb)

            # —— 工具栏: 搜索 / 等级 / 仅已勾选 / 全选 / 计数 ——
            tool = QFrame()
            tool.setObjectName('toolBar')
            tool.setFixedHeight(46)
            tl = QHBoxLayout(tool)
            tl.setContentsMargins(10, 2, 10, 2)
            tl.setSpacing(8)
            search = QLineEdit()
            search.setObjectName('searchBox')
            search.setPlaceholderText(f'搜索 {letter} 盘文件名 / 路径…')
            search.setClearButtonEnabled(True)
            tl.addWidget(search, 1)
            combo = QComboBox()
            combo.setObjectName('filterCombo')
            combo.setFixedWidth(112)
            for label, data in (('全部等级', ''), ('绿色 可删', 'G'),
                                ('橙色 谨慎', 'O'), ('红色 勿删', 'R'),
                                ('可疑 未知', 'S')):
                combo.addItem(label, data)
            tl.addWidget(combo)
            ck_only = QCheckBox('仅已勾选')
            ck_only.setObjectName('toolCheck')
            tl.addWidget(ck_only)
            ck_all = QCheckBox('全选')
            ck_all.setObjectName('toolCheck')
            ck_all.setToolTip('勾选 / 取消当前列表已加载的条目(自动跳过系统关键位置)')
            tl.addWidget(ck_all)
            btn_scope = QToolButton()
            btn_scope.setObjectName('btnSelectScope')
            btn_scope.setIcon(make_gear_icon())
            btn_scope.setIconSize(QSize(16, 16))
            btn_scope.setFixedSize(26, 26)
            btn_scope.setToolTip('设置全选范围')
            btn_scope.clicked.connect(
                lambda _=False, l=letter: self.on_select_scope(l))
            tl.addWidget(btn_scope)
            cnt = QLabel('共 0 项')
            cnt.setObjectName('hint')
            tl.addWidget(cnt)
            v.addWidget(tool)

            # —— 数据仓库 + 目录索引 + 惰性树 ——
            tidx = TreeIndex(letter)
            model = FileTableModel(self)
            model.tidx = tidx
            tree_model = FileTreeModel(model, tidx, self)
            view = QTreeView()
            view.setModel(tree_model)
            view.setEditTriggers(QAbstractItemView.NoEditTriggers)
            view.setSelectionBehavior(QAbstractItemView.SelectRows)
            view.setUniformRowHeights(True)
            view.setExpandsOnDoubleClick(False)   # 双击目录=进入该层, 不走递归 os.walk
            view.setIndentation(18)
            hh = view.header()
            hh.setSectionResizeMode(0, QHeaderView.Stretch)
            for c in (1, 2, 3, 4):
                hh.setSectionResizeMode(c, QHeaderView.Fixed)
            view.setColumnWidth(1, 72)
            view.setColumnWidth(2, 96)
            view.setColumnWidth(3, 210)
            view.setColumnWidth(4, 104)
            view.doubleClicked.connect(
                lambda idx, l=letter: self.on_tree_double_click(idx, l))
            tree_model.checkedChanged.connect(self.update_checked_sum)
            tree_model.checkedChanged.connect(
                lambda l=letter: self._sync_select_all(l))
            v.addWidget(view, 1)

            self.models[letter] = model
            self.tree_indexes[letter] = tidx
            self.tree_models[letter] = tree_model
            self.views[letter] = view
            self.crumb_boxes[letter] = crumb_holder
            self.crumb_ups[letter] = btn_up
            self.filter_level[letter] = combo
            self.filter_checked[letter] = ck_only
            self.chk_all[letter] = ck_all
            self.select_scope_btns[letter] = btn_scope
            self.count_labels[letter] = cnt
            self.search_boxes[letter] = search

            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(320)
            timer.timeout.connect(lambda l=letter: self.apply_filter(l))
            search.textChanged.connect(lambda _t, l=letter: self._filter_timers[l].start())
            combo.currentIndexChanged.connect(lambda _i, l=letter: self.apply_filter(l))
            ck_only.stateChanged.connect(lambda _s, l=letter: self.apply_filter(l))
            ck_all.stateChanged.connect(lambda s, l=letter: self.on_select_all(l, s))
            self._filter_timers[letter] = timer

            # 排序指示器放在字典就绪之后再连, 避免初始化信号打到半成品上
            hh.setSectionsClickable(True)
            hh.setSortIndicatorShown(True)
            hh.sortIndicatorChanged.connect(
                lambda col, order, l=letter: self.on_tree_sort(l, col, order))
            hh.setSortIndicator(2, Qt.DescendingOrder)

            self.pages[letter] = tab
            self.stack.addWidget(tab)
            self._build_crumbs(letter)
        self._update_select_scope_ui()
        main.addWidget(self.stack, 1)
        self.switch_disk(letters[0])

        # 日志
        log_cap = QLabel('操作日志')
        log_cap.setObjectName('logLabel')
        main.addWidget(log_cap)
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName('logBox')
        self.log_box.setReadOnly(True)
        self.log_box.setLineWrapMode(QPlainTextEdit.WidgetWidth)  # 长路径换行, 不被横向裁掉
        self.log_box.setMaximumBlockCount(600)                    # 实时日志不无限增长
        self.log_box.setMaximumHeight(150)
        main.addWidget(self.log_box)

        body.addLayout(main, 1)

        # 侧栏
        side = QVBoxLayout()
        side.setSpacing(10)
        self.btn_scan = QPushButton('刷新扫描')
        self.btn_sum = QPushButton('统计选中大小')
        self.btn_clean = QPushButton('清理选中项')
        self.btn_clean.setObjectName('btnPrimary')
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        self.btn_ai = QPushButton('AI 助手(判断能否删)')
        self.btn_ai_cfg = QPushButton('⚙')
        self.btn_ai_cfg.setFixedWidth(42)
        ai_row.addWidget(self.btn_ai, 1)
        ai_row.addWidget(self.btn_ai_cfg)
        self.btn_ai_apply = QPushButton('选中建议项')
        self.btn_report = QPushButton('导出扫描报告')
        self.btn_exit = QPushButton('退出')
        self.btn_exit.setObjectName('btnGhost')

        for b, h in ((self.btn_scan, 42), (self.btn_sum, 42), (self.btn_clean, 52)):
            b.setFixedHeight(h)
            side.addWidget(b)
        self.btn_ai.setFixedHeight(42)
        self.btn_ai_cfg.setFixedHeight(42)
        side.addLayout(ai_row)
        self.btn_ai_apply.setFixedHeight(42)
        self.btn_ai_apply.setEnabled(False)
        side.addWidget(self.btn_ai_apply)   # 选中建议项: 紧挨 AI 助手下方
        for b, h in ((self.btn_report, 42), (self.btn_exit, 42)):
            b.setFixedHeight(h)
            side.addWidget(b)

        hint = QLabel(
            '· 绿色=安全可删(默认勾选)\n'
            '· 橙色=谨慎删除,清理前确认\n'
            '· 红色=个人/系统文件,请勿删除\n'
            '· 文件树按目录惰性展开, 双击文件夹进入该层\n'
            '· 面包屑点击跳级, 「折叠全部」收起所有展开项\n'
            '· 勾选目录自动包含其全部子文件\n'
            '· 可按等级/已勾选筛选, 全选只作用于当前列表\n'
            '· 点「全选」旁齿轮可设置全选范围(绿/橙/红)\n'
            '· AI 判断: 安全可删/谨慎删除/不可删除')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        side.addWidget(hint)
        side.addStretch(1)
        body.addLayout(side)

        lay.addLayout(body)

        self.btn_scan.clicked.connect(self.on_scan)
        self.btn_sum.clicked.connect(self.on_sum)
        self.btn_clean.clicked.connect(self.on_clean)
        self.btn_ai.clicked.connect(self.on_ai)
        self.btn_ai_cfg.clicked.connect(self.on_ai_config)
        self.btn_ai_apply.clicked.connect(self.on_ai_apply)
        self.btn_report.clicked.connect(self.on_report)
        self.btn_exit.clicked.connect(self.close)

        self.add_log('就绪。点磁盘卡片切换查看; 文件树按目录层级展开, 双击文件夹进入该层, '
                     '面包屑可跳级, 全选/筛选只作用于当前列表; '
                     f'当前全选范围: {"/".join(LEVEL_NAME[v] for v in self.select_all_levels)}'
                     ' (点齿轮可修改)。')

    # ---------- 日志与状态 ----------
    def add_log(self, text):
        self.log_box.appendPlainText(f'[{datetime.now():%H:%M:%S}] {text}')
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_pill(self, text, color=None):
        self.titlebar.pill.setText(text)
        if color:
            self.titlebar.pill.setStyleSheet(
                f'background: {color}; color: white; border-radius: 11px;'
                ' font-size: 8.5pt; font-weight: 700; padding: 3px 12px;')

    def _spin_tick(self):
        # 顺带回收已退出的隔离线程(不依赖跨线程 finished 信号, 更可靠)
        if self._zombies:
            self._zombies[:] = [w for w in self._zombies if w.isRunning()]
        if self._busy not in ('scan', 'ai', 'clean'):
            return
        self._spin = (self._spin + 1) % len(self.SPIN_FRAMES)
        g = self.SPIN_FRAMES[self._spin]
        if self._busy == 'scan':
            self.set_pill(f'扫描中 {g}', C_WARN)
        elif self._busy == 'ai':
            cur, total = self.ai_progress
            tail = f' {cur}/{total} 批' if total else ''
            self.set_pill(f'AI 判断中 {g}{tail}', C_WARN)
            # 待判断行内联"判断中"同步转圈
            for model, path in self.ai_targets:
                v = model.verdicts.get(path)
                if v and v[0].startswith(VERDICT_PEND):
                    model.set_verdict(path, f'{VERDICT_PEND} {g}')
        else:
            self.set_pill(f'清理中 {g}', C_WARN)

    # ---------- 磁盘标签页 / 扫描 ----------
    def switch_disk(self, letter):
        """点击磁盘卡切换下方文件树, 并高亮当前卡片。"""
        page = self.pages.get(letter)
        if page is None:
            return
        self.current_letter = letter
        self.stack.setCurrentWidget(page)
        for l, card in self.disk_cards.items():
            card.set_active(l == letter)
        self._refresh_tree(letter)
        self._build_crumbs(letter)

    # ---------- 文件树 / 面包屑 / 筛选 / 全选 ----------
    def _build_crumbs(self, letter):
        holder = self.crumb_boxes.get(letter)
        tm = self.tree_models.get(letter)
        if holder is None or tm is None:
            return
        while holder.count():
            it = holder.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        parts = tm.root_path.rstrip('\\').split('\\')
        entries = [(letter + ':\\', f'本地磁盘 ({letter}:)')]
        rest = parts[1:] if parts and parts[0].lower() == letter.lower() + ':' else parts
        for i, seg in enumerate(rest, 1):
            p = letter + ':\\' + '\\'.join(rest[:i])
            entries.append((p, seg))
        if len(entries) > 6:
            entries = [entries[0], (None, '…')] + entries[-4:]
        for i, (p, label) in enumerate(entries):
            if i:
                sep = QLabel('›')
                sep.setObjectName('crumbSep')
                sep.setFixedWidth(14)
                sep.setAlignment(Qt.AlignCenter)
                holder.addWidget(sep)
            b = QPushButton(label)
            b.setObjectName('crumbBtn')
            if p is None:
                b.setEnabled(False)
            else:
                b.clicked.connect(lambda _=False, pp=p, l=letter: self.on_crumb(l, pp))
            if i == len(entries) - 1:
                b.setProperty('last', 'true')
                b.style().unpolish(b)
                b.style().polish(b)
            holder.addWidget(b)
        holder.addStretch(1)

    def on_crumb(self, letter, path):
        tm = self.tree_models.get(letter)
        if tm is None or path == tm.root_path:
            return
        tm.set_root(path)
        self.views[letter].collapseAll()
        self._build_crumbs(letter)
        self.update_count_label(letter)
        self._sync_select_all(letter)

    def on_crumb_up(self, letter):
        tm = self.tree_models.get(letter)
        if tm is None:
            return
        p = parent_path(tm.root_path)
        self.on_crumb(letter, p or self.tree_indexes[letter].root)

    def on_tree_sort(self, letter, col, order):
        tm = self.tree_models.get(letter)
        view = self.views.get(letter)
        if tm is None or view is None:
            return
        self._last_sort = (col, order)
        paths = tm.expanded_paths(view)
        tm.sort(col, order)
        tm.restore_expanded(view, paths)

    def on_tree_double_click(self, idx, letter):
        """双击文件夹=进入该层(不再递归 os.walk 整个目录), 双击文件=轻量明细。"""
        tm = self.tree_models[letter]
        node = tm.node(idx)
        if node is tm.root_node:
            return
        row = self.tree_indexes[letter].view_dirs.get(node.path, node.row)
        if row[5] == 'recycle':
            QMessageBox.information(self, '回收站', '回收站使用「清理选中项」清空, 不支持明细浏览。')
            return
        if node.is_dir:
            tm.set_root(node.path)
            self.views[letter].collapseAll()
            self._build_crumbs(letter)
            self.update_count_label(letter)
            self._sync_select_all(letter)
            return
        name = os.path.basename(node.path.rstrip('\\')) or node.path
        FileDetailDialog(self, name, node.path, is_file=True).exec()

    def on_select_all(self, letter, state, quiet=False):
        """全选开关: 只勾选「全选范围」内的等级; 受保护目录自动下钻一层找可勾项。"""
        tm = self.tree_models.get(letter)
        store = self.models.get(letter)
        tidx = self.tree_indexes.get(letter)
        if tm is None or store is None or tidx is None:
            return
        want = want_checked(state)
        scope = set(self.select_all_levels)
        B = chr(92)
        targets, blocked = [], 0

        def add_target(row):
            p = row[0]
            r = tidx.view_dirs.get(p)
            if (r is not None and r[5] == 'container') or row[5] == 'container':
                return                    # 导航容器不是真实条目, 绝不能加入清理
            if rule_level(row[4]) not in scope:
                return                    # 不在用户设定的全选范围内
            targets.append(p)

        if want:
            for row in tidx.view_children.get(tm.root_path, ()):
                p = row[0]
                if is_blocked(p):
                    blocked += 1
                    # 盘根下的 Users/Windows 等受保护目录本身不能删,
                    # 但它们的子项可以, 所以下钻一层把可勾项纳入全选
                    for ch in tidx.view_children.get(p, ())[:1000]:
                        if not is_blocked(ch[0]):
                            add_target(ch)
                else:
                    add_target(row)
            for n in tm.loaded_nodes():
                if not is_blocked(n.path):
                    add_target(n.row)
            for p in dict.fromkeys(targets):
                store.checked.add(p)
        else:
            root = tm.root_path.rstrip(B).lower()
            for p in list(store.checked):
                pl = p.rstrip(B).lower()
                if not (pl == root or pl.startswith(root + B)
                        or root.startswith(pl + B)):
                    continue
                i = store._row_of.get(p)
                if i is None or rule_level(store.rows[i][4]) in scope:
                    store.checked.discard(p)
        for n in tm.loaded_nodes():
            i = tm.createIndex(n.pos, 0, n)
            tm.dataChanged.emit(i, i, [Qt.CheckStateRole])
        if not quiet:
            self.update_checked_sum()
        cb = self.chk_all[letter]
        cb.blockSignals(True)
        cb.setChecked(want)       # 保持用户点击的意图, 不被空判定弹回
        cb.blockSignals(False)
        if quiet:
            return
        verb = '全选' if want else '取消全选'
        n = len(dict.fromkeys(targets)) if want else 0
        extra = f', 另有 {blocked} 个系统关键目录下钻一层' if blocked else ''
        scope_txt = '/'.join(LEVEL_NAME[v] for v in self.select_all_levels)
        self.add_log(f'{letter}: {verb} 当前层级 [范围: {scope_txt}]'
                     + (f' {n:,} 项' if want else ' (已清空)') + extra + '。')

    def _sync_select_all(self, letter):
        """只在「范围内可勾选项是否全被勾选」时同步开关, 避免受保护项让开关自动弹回。"""
        cb = self.chk_all.get(letter)
        tm = self.tree_models.get(letter)
        if cb is None or tm is None:
            return
        scope = set(self.select_all_levels)
        elig = [n for n in tm.loaded_nodes()
                if not is_blocked(n.path) and n.row[5] != 'container'
                and rule_level(n.row[4]) in scope]
        if not elig:
            return                # 顶层没有范围内可勾项时保持开关原状
        cb.blockSignals(True)
        cb.setChecked(all(self.models[letter].is_checked(n.path) for n in elig))
        cb.blockSignals(False)

    # ---------- 全选范围设置 ----------
    def _update_select_scope_ui(self):
        """把当前范围同步到各盘的 tooltip 与齿轮按钮的受限态高亮。"""
        names = '/'.join(LEVEL_NAME[v] for v in self.select_all_levels)
        restricted = len(self.select_all_levels) < len(SELECT_ALL_LEVELS)
        icon = make_gear_icon(color=C_PRIMARY if restricted else C_SUB)
        for cb in self.chk_all.values():
            cb.setToolTip(f'全选范围: {names}\n'
                          '只勾选这些等级的条目(自动跳过系统关键位置与筛选导航目录);\n'
                          '点右侧齿轮可修改范围。')
        for btn in self.select_scope_btns.values():
            btn.setIcon(icon)          # 范围收窄时齿轮转绿, 一眼能看出不是「全部」
            btn.setToolTip(f'设置全选范围\n当前: {names}')
            btn.setProperty('restricted', 'true' if restricted else 'false')
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def set_select_scope(self, levels):
        """设置全选覆盖的等级(全局共享 + 落盘); 范围变窄时同步清掉范围外的已勾选项。"""
        levels = norm_select_levels(levels)
        old = set(self.select_all_levels)
        new = set(levels)
        if old == new:
            self._update_select_scope_ui()
            return
        self.select_all_levels = levels
        prefs = load_ui_prefs()
        prefs['select_all_levels'] = list(levels)
        save_ui_prefs(prefs)

        removed = old - new
        if removed:
            for store in self.models.values():
                for p in list(store.checked):
                    i = store._row_of.get(p)
                    if i is not None and rule_level(store.rows[i][4]) in removed:
                        store.checked.discard(p)
        # 「全选」当前处于开启态的盘, 立刻按新范围重新应用一次
        for letter, cb in self.chk_all.items():
            if cb.isChecked():
                self.on_select_all(letter, Qt.Checked, quiet=True)
            self._sync_select_all(letter)
        for tm in self.tree_models.values():
            tm.refresh_column(0)
        self.update_checked_sum()
        self._update_select_scope_ui()
        names = '/'.join(LEVEL_NAME[v] for v in levels)
        self.add_log(f'全选范围已设为: {names}; 所有磁盘页生效, 已按新范围重新应用。')

    def on_select_scope(self, letter):
        """齿轮按钮: 弹出菜单勾选「全选」要覆盖的等级, 并提供常用预设。"""
        btn = self.select_scope_btns.get(letter)
        if btn is None:
            return
        if self._scope_menu is not None:
            self._scope_menu.deleteLater()   # 避免每次弹菜单都残留一组 QAction
        menu = QMenu(self)
        self._scope_menu = menu       # popup 非阻塞, 必须保持引用防止被 GC
        head = QAction('全选时勾选哪些等级', menu)
        head.setEnabled(False)
        menu.addAction(head)
        menu.addSeparator()
        actions = {}
        for lv in SELECT_ALL_LEVELS:
            a = QAction(f'{LEVEL_NAME[lv]} — {SELECT_LEVEL_HINT[lv]}', menu)
            a.setCheckable(True)
            a.setChecked(lv in self.select_all_levels)
            menu.addAction(a)
            actions[lv] = a

        def on_toggle(_checked, key=None, act=None):
            picked = {k for k, x in actions.items() if x.isChecked()}
            if not picked:               # 至少保留一个等级, 否则「全选」没有意义
                act.setChecked(True)
                picked = {key}
                self.add_log('全选范围至少要保留一个等级。')
            self.set_select_scope(norm_select_levels(picked))

        for lv, a in actions.items():
            a.triggered.connect(
                lambda _c=False, key=lv, act=a: on_toggle(_c, key, act))
        menu.addSeparator()

        def use_preset(key):
            self.set_select_scope(key)
            menu.close()

        for text, lv in (('仅绿色(最安全)', ('G',)),
                         ('绿色 + 橙色', ('G', 'O')),
                         ('全部三级', SELECT_ALL_LEVELS)):
            a = QAction(text, menu)
            a.triggered.connect(lambda _c=False, key=lv: use_preset(key))
            menu.addAction(a)
        note = QAction('注意: 勾选红色会连个人 / 系统文件一起选中', menu)
        note.setEnabled(False)
        menu.addAction(note)

        menu.popup(btn.mapToGlobal(QPoint(0, btn.height())))

    def _filter_active(self, letter):
        return bool((self.filter_level[letter].currentData() or '')
                    or self.filter_checked[letter].isChecked()
                    or self.search_boxes[letter].text().strip())

    def _refresh_tree(self, letter):
        tm = self.tree_models.get(letter)
        view = self.views.get(letter)
        if tm is None or view is None:
            return
        self.tree_indexes[letter].filter_ver += 1
        paths = tm.expanded_paths(view)
        tm.refresh()
        tm.restore_expanded(view, paths)
        self._tree_dirty.discard(letter)
        self.update_count_label(letter)

    def _refresh_dirty_trees(self):
        """扫描中节流刷新: 900ms 一次, 只刷当前可见的盘, 不碰 200 万行全表。"""
        if self._tree_dirty and self.current_letter in self._tree_dirty:
            self._refresh_tree(self.current_letter)
        self._tree_dirty.clear()

    def on_drive_toggled(self, letter):
        """卡片上勾选状态变化: 取消则停止该盘扫描; 勾选空盘则自动补扫一次。"""
        cb = self.drive_checks.get(letter)
        if cb is None:
            return
        if not cb.isChecked():
            w = self.scan_workers.get(letter)
            if w is not None and w.isRunning():
                w.requestInterruption()
                self.add_log(f'{letter}: 盘已取消勾选, 停止扫描(已有结果保留)。')
            return
        w = self.scan_workers.get(letter)
        scanning = w is not None and w.isRunning()
        if (self._busy != 'clean' and self.models[letter].size_hint() == 0
                and not scanning):
            self.add_log(f'{letter}: 盘已勾选, 自动开始扫描…')
            self._start_scan([letter])

    def on_scan(self):
        letters = [l for l, cb in self.drive_checks.items() if cb.isChecked()]
        if not letters:
            QMessageBox.information(self, '提示', '请至少勾选一个磁盘作为扫描范围。')
            return
        self._start_scan(letters)

    def _start_scan(self, letters):
        if self._busy == 'clean':
            QMessageBox.information(self, '提示', '清理进行中, 请稍候再扫描。')
            return
        started = 0
        for letter in letters:
            old = self.scan_workers.pop(letter, None)
            if old is not None and old.isRunning():
                # 断开旧线程的全部信号, 避免它迟到的 batch/done 污染新一轮数据
                for sig, slot in ((old.batch, self.on_scan_batch),
                                  (old.progress, self.on_scan_progress),
                                  (old.trace, self.on_scan_trace),
                                  (old.done, self.on_scan_done),
                                  (old.failed, self.on_scan_failed)):
                    try:
                        sig.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
                old.requestInterruption()
                old.wait(1500)
                if old.isRunning():
                    # 旧线程仍卡在 IO 上: 信号已全部断开, 隔离它并照常启动新线程。
                    # 旧实现在这里 continue 跳过本盘, 导致该盘永远停在"扫描中 0 项"。
                    self._zombies.append(old)
                    self.add_log(f'{letter}: 上一轮扫描线程仍在收尾, 已隔离并重新启动本盘扫描。')
                else:
                    self.add_log(f'{letter}: 盘已中止上一次扫描, 重新开始。')
            self.models[letter].clear()
            tidx = TreeIndex(letter)          # 新扫描换一套全新目录索引
            self.models[letter].tidx = tidx
            self.tree_indexes[letter] = tidx
            tm = self.tree_models[letter]
            tm.tidx = tidx
            tm.set_root(tidx.root)
            self.views[letter].collapseAll()
            self._build_crumbs(letter)
            self._tree_dirty.discard(letter)
            self.scan_trace.pop(letter, None)
            self.update_count_label(letter)
            w = ScanWorker(letter)
            w.batch.connect(self.on_scan_batch)
            w.progress.connect(self.on_scan_progress)
            w.trace.connect(self.on_scan_trace)
            w.done.connect(self.on_scan_done)
            w.failed.connect(self.on_scan_failed)
            self.scan_workers[letter] = w
            w.start()
            started += 1
            self.add_log(f'{letter}: 盘全盘扫描开始(文件+目录)…')
        if started:
            self._busy = 'scan'
            self.btn_clean.setEnabled(False)
        else:
            self.add_log('所选磁盘已在扫描中。')

    def on_scan_batch(self, letter, batch):
        # 只做纯数据追加 + 目录索引, 不再触发任何全表视图刷新
        self.models[letter].append_rows(batch)
        self.tree_indexes[letter].add_batch(batch)
        self._tree_dirty.add(letter)
        self.update_count_label(letter)

    def on_scan_progress(self, letter, n):
        path = self.scan_trace.get(letter, (letter + ':\\', n))[0]
        self.scan_trace[letter] = (path, n)
        self.update_count_label(letter)

    def on_scan_trace(self, letter, path, n):
        """扫描实时日志: 显示当前扫到哪个目录 + 已发现数量(工作线程已按 0.35s 节流)。"""
        self.scan_trace[letter] = (path, n)
        short = path if len(path) <= 96 else path[:44] + '…' + path[-50:]
        self.add_log(f'{letter}: 正在扫描 {short}  ｜ 已发现 {n:,} 项')
        self.update_count_label(letter)

    def on_scan_done(self, letter, total):
        cur = self.scan_workers.get(letter)
        if total < 0 and cur is not None and cur.isRunning():
            # 上一轮被中断线程迟到的 done(-1), 当前盘已在新一轮扫描, 不能把它 pop 掉
            return
        self.scan_workers.pop(letter, None)
        self.scan_trace.pop(letter, None)
        if total >= 0:
            self.add_log(f'{letter}: 盘扫描完成, 共 {total:,} 项。')
        else:
            self.add_log(f'{letter}: 盘扫描被中断。')
        self.update_count_label(letter)
        self.apply_filter(letter)   # 重建筛选视图并最终刷新树, 不再对 200 万行整体排序
        if not any(w.isRunning() for w in self.scan_workers.values()):
            self._busy = None
            self.set_pill('扫描完成', C_OK)
            self.btn_clean.setEnabled(True)
            self.update_checked_sum()

    def on_scan_failed(self, letter, err):
        self.scan_workers.pop(letter, None)
        self.scan_trace.pop(letter, None)
        self.add_log(f'{letter}: 盘扫描失败: {err}')
        self._refresh_tree(letter)
        if not any(w.isRunning() for w in self.scan_workers.values()):
            self._busy = None
            self.set_pill('扫描失败', C_DANGER)
            self.btn_clean.setEnabled(True)

    def update_count_label(self, letter):
        tidx = self.tree_indexes[letter]
        tm = self.tree_models[letter]
        total = self.models[letter].size_hint()
        w = self.scan_workers.get(letter)
        lbl = self.count_labels[letter]
        if w is not None and w.isRunning():
            path, n = self.scan_trace.get(letter, (letter + ':' + chr(92), total))
            n = max(n, total)
            tail = os.path.basename(path.rstrip(chr(92) + '/')) or path
            if len(tail) > 20:
                tail = tail[:18] + '…'
            lbl.setText(f'扫描中… {n:,} 项 · 当前 {tail}')
            return
        if self._filter_active(letter):
            extra = ' · 已截断' if tidx.truncated else ''
            lbl.setText(f'筛选 {tidx.filter_count:,} / 共 {total:,} 项{extra}')
        else:
            shown = len(tidx.view_children.get(tm.root_path, ()))
            lbl.setText(f'{shown:,} / 共 {total:,} 项')

    def apply_filter(self, letter):
        """等级 / 已勾选 / 关键字筛选: 一次全表扫描生成结果树, 再整体刷新。"""
        tidx = self.tree_indexes[letter]
        store = self.models[letter]
        level = self.filter_level[letter].currentData() or ''
        checked_only = self.filter_checked[letter].isChecked()
        text = self.search_boxes[letter].text()
        active = bool(level or checked_only or text.strip())
        if active:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            tidx.build_filter(store, level, checked_only, text)
            tm = self.tree_models[letter]
            view = self.views[letter]
            if active:
                tm.set_root(tidx.root)
                view.collapseAll()
                self._build_crumbs(letter)
            else:
                paths = tm.expanded_paths(view)
                tm.refresh()
                tm.restore_expanded(view, paths)
        finally:
            if active:
                QApplication.restoreOverrideCursor()
        self._tree_dirty.discard(letter)
        self.update_count_label(letter)
        if tidx.truncated:
            self.add_log(f'{letter}: 筛选结果超过 {tidx.MAX_FILTER:,} 条, '
                         f'仅显示前 {tidx.MAX_FILTER:,} 条。')

    # ---------- 勾选统计 ----------
    def collect_checked(self, dedupe=True):
        items = []
        for m in self.models.values():
            items.extend(m.checked_entries())
        return dedupe_entries(items) if dedupe else items

    def update_checked_sum(self, *args):
        """防抖: 连续勾选 / 全选不会每次都扫一遍全表。"""
        self._sum_timer.start()

    def _do_update_checked_sum(self):
        pairs = [(m, p) for m in self.models.values() for p in m.checked]
        fast = len(pairs) > 120000
        kept = []
        for m, p in pairs:
            if not fast and m.checked_ancestor(parent_path(p)):
                continue          # 祖先目录已勾选, 子项不重复计算
            r = m.row_by_path(p)
            if r is not None:
                kept.append(r)
        total = sum(e[2] for e in kept)
        self.sum_value.setText(('约 ' if fast else '') + fmt_size(total))
        n_green = sum(1 for e in kept if rule_level(e[4]) == 'G')
        n_orange = sum(1 for e in kept if rule_level(e[4]) == 'O')
        n_red = sum(1 for e in kept if rule_level(e[4]) == 'R')
        self.sum_sub.setText(f'共 {len(kept):,} 项  绿 {n_green} / 橙 {n_orange} / 红 {n_red}')

    # ---------- 明细 ----------
    def on_show_detail(self, tree_index, letter):
        """兼容旧入口: 现在双击统一走 on_tree_double_click。"""
        self.on_tree_double_click(tree_index, letter)

    # ---------- 统计 ----------
    def on_sum(self):
        items = self.collect_checked(dedupe=True)
        if not items:
            QMessageBox.information(self, '统计', '尚未勾选任何条目。')
            return
        total = sum(e[2] for e in items)
        n = {'G': 0, 'O': 0, 'R': 0}
        for e in items:
            n[rule_level(e[4])] += 1
        top = sorted(items, key=lambda e: e[2], reverse=True)[:8]
        lines = [f'勾选 {len(items):,} 项, 合计 {fmt_size(total)}',
                 f'(已去重: 勾选目录内的文件不重复计算)',
                 f'绿色 {n["G"]} / 橙色 {n["O"]} / 红色 {n["R"]}', '', '最大的几项:']
        for e in top:
            lines.append(f'  {fmt_size(e[2]):>10}  {e[0]}')
        QMessageBox.information(self, '统计选中大小', '\n'.join(lines))

    # ---------- 清理 ----------
    def on_clean(self):
        if self._busy == 'clean':
            return
        items = self.collect_checked(dedupe=True)
        if not items:
            QMessageBox.information(self, '清理', '尚未勾选任何条目。')
            return
        blocked = [e for e in items if e[5] != 'recycle' and is_blocked(e[0])]
        items = [e for e in items if not (e[5] != 'recycle' and is_blocked(e[0]))]
        if blocked:
            self.add_log(f'安全红线拦截 {len(blocked)} 项, 不会删除。')
        if not items:
            QMessageBox.warning(self, '清理', '勾选项全部命中安全红线, 已取消。')
            return

        risky = [e for e in items if rule_level(e[4]) != 'G']
        total = sum(e[2] for e in items)
        msg = [f'即将清理 {len(items):,} 项, 约 {fmt_size(total)}。']
        if risky:
            msg.append(f'其中橙/红色 {len(risky)} 项(含回收站清空), 删除后可能不可恢复!')
            msg.append('')
            for e in risky[:10]:
                msg.append(f'  · {e[0]}')
            if len(risky) > 10:
                msg.append(f'  … 等共 {len(risky)} 项')
        msg.append('')
        msg.append('确认删除?')
        r = QMessageBox.warning(self, '确认清理', '\n'.join(msg),
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            self.add_log('用户取消了清理。')
            return

        self._busy = 'clean'
        self.btn_clean.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.add_log(f'开始清理 {len(items):,} 项…')
        self.clean_worker = CleanWorker(items)
        self.clean_worker.log.connect(self.add_log)
        self.clean_worker.done.connect(self.on_clean_done)
        self.clean_worker.start()

    def on_clean_done(self, freed, before, after, deleted):
        invalidate_recycle_cache()   # 回收站可能刚被清空, 下次扫描重新读取
        self._busy = None
        self.btn_clean.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.set_pill('清理完成', C_OK)
        for m in self.models.values():
            m.remove_paths(set(deleted))
        for tidx in self.tree_indexes.values():
            tidx.filter_ver += 1
        self.apply_filter(self.current_letter)   # 重建视图, 已删除条目从树里消失
        self.update_checked_sum()
        for d in list_fixed_drives():
            card = self.disk_cards.get(d['Letter'])
            if card:
                card.update_disk(d['FreeGB'], d['TotalGB'])
        delta = ', '.join(f'{k}: {before.get(k, 0)}→{after.get(k, 0)} GB'
                          for k in sorted(set(before) | set(after)))
        self.add_log(f'清理完成, 释放约 {fmt_size(freed)}。剩余空间 {delta}')

    # ---------- AI ----------
    def on_ai_config(self):
        dlg = AiConfigDialog(self, self.ai_cfg)
        if dlg.exec() == QDialog.Accepted:
            self.add_log(f'AI 配置已保存: {self.ai_cfg.base_url} / {self.ai_cfg.model}')

    def _refresh_ai_column(self):
        """只刷新树里已加载节点的 AI判断 列, 不再对 200 万行发全表 dataChanged。"""
        for tm in self.tree_models.values():
            tm.refresh_column(4)

    def on_ai(self):
        if self._busy == 'ai':
            return
        if self._busy in ('scan', 'clean'):
            QMessageBox.information(self, '提示', '请等待当前扫描/清理完成后再进行 AI 判断。')
            return
        if not self.ai_cfg.api_key:
            self.on_ai_config()
            if not self.ai_cfg.api_key:
                return

        cfg = self.ai_cfg
        scope = norm_ai_scope(cfg.scope)
        if cfg.use_cache:
            ai_cache_load(cfg.model)
        else:
            AI_GROUP_VERDICTS.clear()

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            groups = build_ai_groups(self.models, scope, cfg.max_groups,
                                     self.current_letter, tick=QApplication.processEvents)
        finally:
            QApplication.restoreOverrideCursor()
        if not groups:
            QMessageBox.information(self, 'AI 助手', '当前判断范围内没有扫描结果, 请先扫描或放宽范围。')
            return

        pending = [g for g in groups if g['key'] not in AI_GROUP_VERDICTS]
        cached = len(groups) - len(pending)
        if not pending:
            self._refresh_ai_column()
            self.btn_ai_apply.setEnabled(True)
            self.add_log(f'AI 判断: {cached} 个模式全部命中本机缓存, 本次 0 次请求 / 0 token。')
            QMessageBox.information(self, 'AI 助手',
                                    f'全部 {cached} 个文件模式已有本机判定缓存。\n'
                                    '本次没有发起任何请求, 不消耗 token。\n'
                                    '如需重新判断, 请在 AI 设置里点「清除缓存」。')
            return

        self.ai_batches = make_ai_batches(pending, self.AI_BATCH_CHARS,
                                          self.AI_BATCH_GROUPS)
        in_tokens = sum(estimate_tokens(AI_SYSTEM_PROMPT + p)
                        for p, _ in self.ai_batches)
        out_tokens = len(pending) * 14
        covered = sum(g['count'] for g in pending)
        scope_name = AI_SCOPES[scope][1]
        risky_note = ('\n注意: 红色 / 可疑项只会给出判断建议, 不会被「选中建议项」自动勾选。'
                      if scope in ('RISK', 'R', 'SUS') else '')

        box = QMessageBox(self)
        box.setWindowTitle('AI 判断 — Token 预算')
        box.setMinimumWidth(560)
        box.setIcon(QMessageBox.Information)
        box.setText(f'按「目录模式」归类后, AI 只判断最值得判断的 {len(groups)} 组, '
                    f'每组结论自动套用到同组的全部文件。')
        box.setInformativeText(
            f'判断范围：{scope_name}\n'
            f'待判断 {len(pending)} 组（缓存命中 {cached} 组，不再付费）\n'
            f'覆盖约 {covered:,} 个文件 / 目录\n'
            f'分 {len(self.ai_batches)} 批请求 · 输入约 {in_tokens:,} + 输出约 {out_tokens:,} '
            f'≈ {in_tokens + out_tokens:,} tokens（粗估）\n'
            f'{risky_note}\n'
            '点「开始判断」才会发起付费请求。')
        b_run = box.addButton('开始判断', QMessageBox.AcceptRole)
        b_cfg = box.addButton('调整预算', QMessageBox.ActionRole)
        box.addButton('取消', QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_cfg:
            self.on_ai_config()
            return
        if box.clickedButton() is not b_run:
            return

        by_key = {g['key']: g for g in groups}
        self.ai_results = {}
        self.ai_targets = []          # 代表行, 供「判断中」转圈
        self.ai_reps_by_batch = []
        for _prompt, keys in self.ai_batches:
            reps = []
            for k in keys:
                g = by_key[k]
                m = self.models[g['letter']]
                m.set_verdict(g['path'], VERDICT_PEND)
                reps.append((m, g['path']))
                self.ai_targets.append((m, g['path']))
            self.ai_reps_by_batch.append(reps)

        self.ai_progress = (0, len(self.ai_batches))
        self._busy = 'ai'
        self.btn_ai.setEnabled(False)
        self.btn_ai_apply.setEnabled(False)
        self.add_log(f'AI 判断开始: {len(pending)} 个模式 / {len(self.ai_batches)} 批, '
                     f'预估 {in_tokens + out_tokens:,} tokens, 覆盖 {covered:,} 项。')
        self.ai_worker = AiWorker(cfg, batches=[p for p, _ in self.ai_batches])
        self.ai_worker.batch_done.connect(self.on_ai_batch_done)
        self.ai_worker.progress.connect(self.on_ai_progress)
        self.ai_worker.all_done.connect(self.on_ai_all_done)
        self.ai_worker.failed.connect(self.on_ai_failed)
        self.ai_worker.start()

    def on_ai_progress(self, cur, total):
        self.ai_progress = (cur, total)

    def on_ai_batch_done(self, text, idx):
        if idx >= len(self.ai_batches):
            return
        keys = self.ai_batches[idx][1]
        parsed = parse_ai_reply(text)
        hit = 0
        for i, key in enumerate(keys, 1):
            v = parsed.get(i)
            if v:
                self.ai_results[key] = v
                AI_GROUP_VERDICTS[key] = v
                hit += 1
        for m, path in self.ai_reps_by_batch[idx]:
            m.clear_verdict(path)
        self._refresh_ai_column()
        self.add_log(f'AI 第 {idx + 1}/{len(self.ai_batches)} 批: 解析 {hit}/{len(keys)} 个模式结论。')
        if hit == 0:
            self.add_log(f'（第 {idx + 1} 批原始回复）{text[:400]}')

    def _ai_finish(self, err=None):
        self._busy = None
        self.btn_ai.setEnabled(True)
        for reps in self.ai_reps_by_batch:
            for m, path in reps:
                m.clear_verdict(path)
        self.ai_targets = []
        hit = len(self.ai_results)
        if hit and self.ai_cfg.use_cache:
            ai_cache_save(self.ai_cfg.model, self.ai_results)
        self._refresh_ai_column()
        self.btn_ai_apply.setEnabled(hit > 0)
        if err:
            self.set_pill('AI 请求失败', C_DANGER)
            self.add_log(f'AI 请求失败: {err}（已完成的 {hit} 个模式结论已保留）')
            QMessageBox.warning(self, 'AI 助手',
                                f'请求失败（已完成批次的结果仍有效）:\n{err}')
        else:
            self.set_pill(f'AI 完成 {hit} 模式', C_OK)
            self.add_log(f'AI 判断完成: {hit} 个模式结论已套用到同组全部文件; '
                         '点「选中建议项」按"安全可删"一键勾选。')

    def on_ai_all_done(self, _ok):
        self._ai_finish()

    def on_ai_failed(self, err):
        self._ai_finish(err)

    def on_ai_apply(self):
        # 模式结论覆盖全盘, 先列出待变更项并让用户确认, 避免一次静默勾选几十万行
        # 红色 / 可疑项即使被 AI 判「安全可删」也只给建议、不自动勾选(安全边界)。
        adds, removes, risky = [], [], 0
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for m in self.models.values():
                for row in m.rows:
                    path, size = row[0], row[2]
                    v = m.verdict_of(row)[0]
                    if v == VERDICT_SAFE and path not in m.checked:
                        if rule_level(row[4]) == 'R' or is_blocked(path):
                            risky += 1
                        else:
                            adds.append((m, path, size))
                    elif v == VERDICT_NO and path in m.checked:
                        removes.append((m, path))
        finally:
            QApplication.restoreOverrideCursor()
        if not adds and not removes:
            if risky:
                QMessageBox.information(
                    self, '选中建议项',
                    f'AI 对 {risky:,} 个红色 / 可疑项给出了建议, '
                    '但这类项目不会被自动勾选, 请在文件树中手动确认。')
            else:
                QMessageBox.information(self, '选中建议项', '没有需要变更的勾选项。')
            return
        msg = [f'将新增勾选 {len(adds):,} 项（约 {fmt_size(sum(a[2] for a in adds))}）,',
               f'取消勾选 {len(removes):,} 项。',
               '',
               'AI 结论按目录模式批量套用, 覆盖面比逐项判断大得多;',
               '橙色项在清理时仍会二次确认, 系统关键位置始终禁止删除。']
        if risky:
            msg.append(f'红色 / 可疑项有 {risky:,} 项仅给出判断建议, 不会被自动勾选, 需手动确认。')
        msg += ['', '继续?']
        if QMessageBox.question(self, '确认按 AI 建议勾选', '\n'.join(msg),
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        for m, path, _size in adds:
            m.checked.add(path)
        for m, path in removes:
            m.checked.discard(path)
        for tm in self.tree_models.values():
            tm.refresh_column(0)
        self.update_checked_sum()
        self._sync_select_all(self.current_letter)
        self.add_log(f'已按 AI 建议勾选 {len(adds):,} 项"安全可删", '
                     f'取消 {len(removes):,} 项"不可删除"; '
                     f'红色/可疑 {risky:,} 项仅给出建议, 未自动勾选。')

    # ---------- 报告 ----------
    def on_report(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出扫描报告',
                                              f'磁盘扫描报告_{datetime.now():%Y-%m-%d_%H%M}.md',
                                              'Markdown (*.md)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.build_report_text())
            self.add_log(f'报告已导出: {path}')
        except OSError as e:
            QMessageBox.warning(self, '导出失败', str(e))

    def build_report_text(self) -> str:
        lines = [f'==== 磁盘扫描报告 {datetime.now():%Y-%m-%d %H:%M:%S} ====']
        for d in list_fixed_drives():
            lines.append(f"{d['Letter']}: 可用 {d['FreeGB']} GB / 共 {d['TotalGB']} GB")
        for letter, m in self.models.items():
            n = {'G': 0, 'O': 0, 'R': 0}
            for r in m.rows:
                n[rule_level(r[4])] += 1
            lines.append('')
            lines.append(f'---- {letter} 盘: 共 {len(m.rows):,} 项  '
                         f'绿 {n["G"]} / 橙 {n["O"]} / 红 {n["R"]} ----')
            lines.append('-- 最大的 100 项 --')
            for r in sorted(m.rows, key=lambda x: x[2], reverse=True)[:100]:
                verdict = m.verdict_of(r)[0]
                lines.append(f'{fmt_size(r[2]):>10}  {LEVEL_NAME[rule_level(r[4])]}  '
                             f'{r[0]}  [{verdict}]')
            checked = dedupe_entries(m.checked_entries())
            if checked:
                lines.append(f'-- 已勾选 {len(checked):,} 项, 合计 '
                             f'{fmt_size(sum(e[2] for e in checked))} --')
        return '\n'.join(lines)

    def closeEvent(self, ev):
        for w in list(self.scan_workers.values()):
            if w.isRunning():
                w.requestInterruption()
                w.wait(3000)
        for w in list(self._zombies):
            if w.isRunning():
                w.requestInterruption()
                w.wait(1000)
        for w in (self.clean_worker, self.ai_worker):
            if w is not None and w.isRunning():
                w.requestInterruption()
                w.wait(3000)
        super().closeEvent(ev)


# ==================== 无窗口自检 ====================

def selftest():
    assert fmt_size(0) == '0 B'
    assert fmt_size(2048) == '2 KB'
    assert fmt_size(5 * 1048576) == '5.0 MB'
    # 全选范围: 归一化 / 至少保留一项 / 落盘 round-trip
    import tempfile
    assert norm_select_levels('GO') == ('G', 'O')
    assert norm_select_levels(['R', 'G', 'R', 'X']) == ('G', 'R')
    assert norm_select_levels([]) == SELECT_ALL_LEVELS
    assert norm_select_levels(None) == SELECT_ALL_LEVELS
    global UI_PREFS_FILE
    old_prefs = UI_PREFS_FILE
    try:
        UI_PREFS_FILE = os.path.join(tempfile.gettempdir(), 'dc_selftest_prefs.json')
        save_ui_prefs({'select_all_levels': ['G', 'R']})
        assert norm_select_levels(load_ui_prefs().get('select_all_levels')) == ('G', 'R')
    finally:
        try:
            os.remove(UI_PREFS_FILE)
        except OSError:
            pass
        UI_PREFS_FILE = old_prefs
    # 分级
    cases = [
        (r'C:\Users\me\AppData\Local\Temp\x.log', 'G'),
        (r'C:\Users\me\AppData\Local\pip\cache\x.whl', 'G'),
        (r'D:\MyCode\proj\node_modules\react\index.js', 'O'),
        (r'D:\MyCode\proj\node_modules', 'O'),          # 目录本体(无尾分隔符)
        (r'C:\Users\me\.gradle\caches\x', 'O'),
        (r'C:\Users\me\.gradle', 'O'),
        (r'C:\Program Files\App\app.exe', 'O'),
        (r'C:\Users\me\Documents\report.docx', 'R'),
        (r'C:\Windows\System32\ntdll.dll', 'R'),
        (r'D:\MyCode\proj\.git\objects\x', 'R'),
        (r'D:\MyCode\proj\main.py', 'R'),
    ]
    for p, lv in cases:
        got = rule_level(classify_rule(p))
        assert got == lv, f'{p}: expect {lv} got {got}'
    # 小树遍历
    import tempfile
    root = tempfile.mkdtemp(prefix='dc_selftest_')
    try:
        os.makedirs(os.path.join(root, 'sub', '__pycache__'))
        with open(os.path.join(root, 'a.bin'), 'wb') as f:
            f.write(b'x' * 1024)
        with open(os.path.join(root, 'sub', '__pycache__', 'm.pyc'), 'wb') as f:
            f.write(b'y' * 2048)
        rows = walk_path(root)
        assert any(r[0].endswith('a.bin') for r in rows)
        dirs = [r for r in rows if r[1]]
        pyc_dir = [r for r in dirs if r[0].lower().endswith('__pycache__')]
        assert pyc_dir and pyc_dir[0][2] == 2048, pyc_dir
        assert rule_level(pyc_dir[0][4]) == 'G'
    finally:
        shutil.rmtree(root, ignore_errors=True)
    # AI 回复解析
    r = parse_ai_reply('1 | 安全可删 | 缓存\n2 | 谨慎删除 | 依赖\n3 | 不可删除 | 源码')
    assert r[1][0] == VERDICT_SAFE and r[2][0] == VERDICT_CARE and r[3][0] == VERDICT_NO
    # AI 省 token: 目录模式归一化(跨盘/跨用户/跨随机名同组)
    k1 = ai_group_key(r'C:\Users\me\AppData\Local\Temp\a1.tmp', 1, False)
    k2 = ai_group_key(r'D:\Users\you\AppData\Local\Temp\zz99.tmp', 1, False)
    assert k1 == k2, (k1, k2)
    assert ai_group_key(r'C:\Users\me\Documents\report.docx', 9, False) != k1
    assert ai_group_key(r'C:\Program Files\App\124.0.1\x.dll', 5, False) == \
        ai_group_key(r'C:\Program Files\App\125.1.2\y.dll', 5, False)

    class _M:
        def __init__(self, rows):
            self.rows = rows

    B = chr(92)
    rows = []
    for i in range(300):
        rows.append((f'D:{B}MyCode{B}proj{B}node_modules{B}pkg{i % 3}{B}f{i}.js',
                     False, 4096, 0.0, 3, ''))
        rows.append((f'D:{B}MyCode{B}proj{B}.git{B}objects{B}o{i}',
                     False, 8192, 0.0, 11, ''))
        rows.append((f'C:{B}Users{B}me{B}AppData{B}Local{B}Temp{B}t{i}.tmp',
                     False, 1024, 0.0, 1, ''))
    groups = build_ai_groups({'C': _M(rows), 'D': _M([])}, 'GO', 120)
    assert groups and len(groups) < 10, len(groups)          # 900 行压成个位数模式
    assert sum(g['count'] for g in groups) == 600            # 红色 .git 被 GO 范围排除
    # AI 判断范围: 新增「红色 / 橙色 / 可疑」三类与组合范围
    assert norm_ai_scope('risk') == 'RISK' and norm_ai_scope('bogus') == 'GO'
    assert all(c in AI_SCOPES for c in AI_SCOPE_ORDER)
    assert is_suspicious(12) and not is_suspicious(9)
    assert not scope_accepts('RISK', 'G', 0)
    assert scope_accepts('RISK', 'O', 3) and scope_accepts('RISK', 'R', 9)
    assert scope_accepts('RISK', 'R', 12)
    assert scope_accepts('R', 'R', 9) and not scope_accepts('R', 'R', 12)
    assert scope_accepts('SUS', 'R', 12) and not scope_accepts('SUS', 'R', 9)
    assert scope_accepts('O', 'O', 3) and not scope_accepts('O', 'G', 0)
    rows2 = rows + [(f'D:{B}Data{B}misc{B}file{i}.dat',
                     False, 2048, 0.0, 12, '') for i in range(300)]
    m2 = {'C': _M(rows2), 'D': _M([])}
    cover = {s: sum(g['count'] for g in build_ai_groups(m2, s, 120))
             for s in ('GO', 'OO', 'RISK', 'R', 'O', 'SUS', 'ALL')}
    assert cover == {'GO': 600, 'OO': 600, 'RISK': 900, 'R': 300,
                     'O': 300, 'SUS': 300, 'ALL': 1200}, cover
    assert build_ai_groups(m2, 'CUR', 120, current_letter='D') == []
    batches = make_ai_batches(groups * 30, max_chars=1200, per_batch=15)
    assert batches and sum(len(keys) for _, keys in batches) == len(groups) * 30
    assert all(len(keys) <= 15 for _, keys in batches)
    assert all(p.splitlines()[1].startswith('1 | ') for p, _ in batches)
    assert estimate_tokens('a' * 300) == 101
    # 判定缓存 round-trip(临时文件, 不污染真实缓存)
    global AI_CACHE_FILE
    old_cache = AI_CACHE_FILE
    try:
        AI_CACHE_FILE = os.path.join(tempfile.gettempdir(), 'dc_selftest_ai_cache.json')
        ai_cache_clear()
        ai_cache_save('model-a', {k1: (VERDICT_SAFE, '临时文件')})
        ai_cache_save('model-b', {k1: (VERDICT_NO, '别删')})
        ai_cache_load('model-a')
        assert AI_GROUP_VERDICTS.get(k1, (None,))[0] == VERDICT_SAFE
        ai_cache_load('model-b')
        assert AI_GROUP_VERDICTS.get(k1, (None,))[0] == VERDICT_NO
        ai_cache_clear()
        assert not AI_GROUP_VERDICTS
    finally:
        AI_CACHE_FILE = old_cache
        AI_GROUP_VERDICTS.clear()
    assert is_blocked('C:\\Windows')
    assert is_blocked('C:\\')
    assert not is_blocked('C:\\Windows\\Temp')
    # 文件树: 路径索引 / 占位目录 / 勾选继承 / 筛选 / 惰性分批
    B = chr(92)
    assert parent_path('C:' + B + 'Users') == 'C:' + B
    assert parent_path('C:' + B + 'Users' + B + 'a.txt') == 'C:' + B + 'Users'
    assert parent_path('C:' + B) == ''
    assert want_checked(2) and want_checked(Qt.Checked)
    assert not want_checked(0) and not want_checked(Qt.Unchecked)
    tstore = FileTableModel()
    tidx = TreeIndex('C')
    tstore.tidx = tidx
    trows = [
        ('C:' + B + 'Users', True, 100, 0.0, 12, ''),
        ('C:' + B + 'Users' + B + 'me', True, 80, 0.0, 12, ''),
        ('C:' + B + 'Users' + B + 'me' + B + 'a.tmp', False, 20, 0.0, 1, ''),
        ('C:' + B + 'Windows' + B + 'x.tmp', False, 5, 0.0, 1, ''),
        ('C:' + B + 'Windows' + B + 'doc.docx', False, 7, 0.0, 9, ''),
    ]
    tstore.append_rows(trows)
    tidx.add_batch(trows)
    assert len(tidx.children['C:' + B]) == 2
    assert len(tidx.children['C:' + B + 'Users']) == 1
    assert tidx.dirs.get('C:' + B + 'Windows')       # 目录行晚到时的占位节点
    tstore.checked.clear()                           # 去掉绿色默认勾选, 单测继承
    assert tstore.checked_ancestor(trows[2][0]) is None
    tstore.set_checked('C:' + B + 'Users' + B + 'me', True)
    assert tstore.is_checked(trows[2][0])
    assert len(tstore.checked_entries()) == 1
    tidx.build_filter(tstore, level='R')
    assert tidx.filter_count == 3                   # 红色 = 2 个规则12 + 1 个规则9
    tidx.build_filter(tstore, level='S')             # 可疑 = 仅规则 12 未知文件
    assert tidx.filter_count == 2
    tidx.build_filter(tstore, text='a.tmp')
    assert tidx.filter_count == 1
    tidx.build_filter(tstore, checked_only=True)
    assert tidx.filter_count >= 1
    tidx.build_filter(tstore)
    assert tidx.filter_count == 0
    tm = FileTreeModel(tstore, tidx)
    assert tm.rowCount(QModelIndex()) == 0           # 未 fetch 前不建任何 TreeNode
    tm.fetchMore(QModelIndex())
    assert tm.rowCount(QModelIndex()) == 2
    assert tm.data(tm.index(0, 0), Qt.DisplayRole)
    tm.sort(0, Qt.AscendingOrder)
    assert tm.rowCount(QModelIndex()) == 2
    print('SELFTEST_OK no window, no user-data deletion')


# ==================== 启动 ====================

def make_check_png(path: str):
    """运行时生成勾选图标(避免引入资源文件), 供 QSS 引用。"""
    pm = QPixmap(22, 22)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor('white'), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(5, 11, 9.5, 15.5)
    p.drawLine(9.5, 15.5, 16, 7)
    p.end()
    pm.save(path)


def make_gear_icon(size=20, color='#6B7280') -> QIcon:
    """运行时自绘齿轮图标, 不依赖字体里是否有 ⚙ 字形。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.translate(size / 2.0, size / 2.0)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    tooth_w, r_in, r_out = size * 0.20, size * 0.25, size * 0.45
    for i in range(8):
        p.save()
        p.rotate(i * 45)
        p.drawRoundedRect(QRectF(-tooth_w / 2, -r_out, tooth_w,
                                 r_out - r_in + 1), 1.0, 1.0)
        p.restore()
    p.drawEllipse(QRectF(-r_in - 1.5, -r_in - 1.5,
                         2 * (r_in + 1.5), 2 * (r_in + 1.5)))
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    hole = size * 0.12
    p.drawEllipse(QRectF(-hole, -hole, 2 * hole, 2 * hole))
    p.end()
    return QIcon(pm)


def main():
    if '--selftest' in sys.argv:
        selftest()
        return

    def _excepthook(tp, val, tb):
        import traceback
        log = os.path.join(APP_DATA_DIR, 'disk_cleaner_gui.log')
        try:
            with open(log, 'a', encoding='utf-8') as f:
                f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] unhandled:\n")
                traceback.print_exception(tp, val, tb, file=f)
        except OSError:
            pass
        if sys.stderr:                 # --windowed 打包后 sys.stderr 可能为 None
            traceback.print_exception(tp, val, tb)
    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    # 应用图标: 让窗口标题栏与任务栏显示自定义翡翠绿磁盘图标(否则只显示 Qt 默认)
    # 运行位置: 源码=脚本目录; 打包 onedir=exe 同级或 _internal; onefile=临时解压目录(_MEIPASS)
    if getattr(sys, 'frozen', False):
        _base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        _icon_cands = [os.path.join(os.path.dirname(sys.executable), 'disk_cleaner.ico'),
                       os.path.join(_base, 'disk_cleaner.ico')]
    else:
        _icon_cands = [os.path.join(APP_DATA_DIR, 'disk_cleaner.ico')]
    for _ip in _icon_cands:
        if os.path.exists(_ip):
            app.setWindowIcon(QIcon(_ip))
            break
    check_png = os.path.join(os.environ.get('TEMP', '.'), 'dc_check.png')
    try:
        make_check_png(check_png)
        url = check_png.replace(os.sep, '/')
        app.setStyleSheet(QSS + "\nQCheckBox::indicator:checked, QTableWidget::indicator:checked,"
                              " QTableView::indicator:checked, QTreeView::indicator:checked"
                              "{ image: url(" + url + "); }")
    except Exception:  # noqa: BLE001
        pass
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
