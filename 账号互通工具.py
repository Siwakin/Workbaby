# -*- coding: utf-8 -*-
"""
WorkBaby 账号及会话管理助手（跨账号迁移 / 复制会话）
（原名 WorkBuddy 账号互通工具；非官方第三方工具，与腾讯无从属关系）
====================================================

解决什么问题
------------
一台电脑上登录过两个 WorkBuddy 账号（比如“家用账号”和“学校账号”），
两边看到的历史会话（任务）互不相通：在 A 账号里做了一半的任务，
切换到 B 账号后找不到、也没法接着做。

原理（只改归属，不动聊天内容）
------------------------------
* 会话列表保存在  ~/.workbuddy/workbuddy.db  的 sessions 表里，
  每一行有 user_id 字段 —— 应用只给“当前登录账号”显示它自己的行。
* 会话底稿（~/.workbuddy/projects/<文件夹>/<会话ID>.jsonl）是整盘共用的，
  不与账号绑定。
所以：把 sessions 行的 user_id 改成另一个账号，那个账号登录后
就能在列表里看到这个会话，点开就能接着聊（内容原样保留）。

三种用法
--------
1) 迁移（交接）：选中的会话过继给目标账号，原账号不再显示。
   → 适合“这个任务我不做了，交给另一个账号继续”。
2) 复制（双边可见）：给目标账号生成一个“分身会话”（新 ID + 底稿拷贝），
   两个账号都能打开自己那一份继续聊。
   → 适合“两个账号都要用这个任务，各聊各的”。
   → 注意：复制之后两边内容不再自动同步，只是开局相同。
3) 定时任务归属迁移：把 automations（自动化/监测任务）的 owner_user_id
   过户给目标账号。

安全约束（重要）
----------------
* 运行本工具前必须【完全退出 WorkBuddy】，否则应用会把我们的修改覆盖掉。
  工具顶部会检测，没退出不让动手。
* 每次写库前自动把 workbuddy.db（含 -wal/-shm）备份到“数据库备份”目录，
  全程写“迁移日志.txt”。出问题把备份拷回去即可还原。
* 本工具不碰登录凭证（在 Windows 凭祖管理器里），切换账号请用
  WorkBuddy 自带的“切换账号 / 退出登录”。
* transport=cloud 的云端会话跟随服务器账号，不能本地迁移（工具会拦）。
"""

import os
import re
import sys
import json
import uuid
import stat
import math
import shutil
import sqlite3
import datetime
import subprocess
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter.scrolledtext import ScrolledText

# 让同目录的 wb_usage / wb_crypto 一定能被 import（不管从哪个工作目录启动）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _app_dir():
    """程序“自己的目录”。

    ★ 打包成 exe 后 __file__ 指向 PyInstaller 解包出来的临时目录（_MEIPASS），
      那里每次启动都会被清空重建，绝不能往里写配置/日志/快照。
      所以 exe 模式下必须用 sys.executable 所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# 用量统计 / 加密导出（同目录的模块；缺了也不影响老的迁移功能）
try:
    import wb_usage
except Exception:                                    # pragma: no cover
    wb_usage = None
try:
    import wb_crypto
except Exception:                                    # pragma: no cover
    wb_crypto = None

APP_TITLE = "WorkBaby 账号及会话管理助手"
# ★ 版本号规则（2026-10-26 用户约定）：小更新末位 +1 → v3.02、v3.03 …
#   大改版才跳到 v4.0。exe 文件名同步带这个号，便于区分新旧。
APP_VER = "v3.12"

# ---- 界面配色（科技风 · 冷黑底 + 青色冷光，v3.05 重新校准）
#
# ★ 为什么又调一次（2026-10-26 用户：“颜色和字色有点不搭”）：
#   v3.04 的毛病是「彩度不统一」——强调青 sat=0.82、红字 sat=0.70，
#   而次要文字只有 0.27，同一屏里要在霓虹色和灰雾之间反复切换，眼睛很累；
#   加上所有文字都偏蓝（蓝通道接近 255），压在蓝黑底上边界发虚；
#   斑马纹两行对比只有 1.06，等于没有，表格糊成一片。
#
# ★ 新规则（三层分离）：
#   1) 背景层：冷黑，但蓝通道收敛（不再一味加深蓝）
#   2) 文字层：中性偏冷（sat 0.06~0.31），四档拉开，靠明度分层不靠色相
#   3) 强调层：只留青 + 电蓝两个同族色，紫、橙全部清掉；状态色降饱和
#
# ---- 背景层（越底层越暗，靠描边区分层次而不是靠亮块）
C_PAGE = "#070b12"        # 页面底色
C_CARD = "#0e1826"        # 卡片 / 表格底
C_CARD2 = "#14202f"       # 次级面板 / 表头
C_INPUT = "#0a121b"       # 输入框底
C_LINE = "#1e2d40"        # 分隔线（比卡片亮 1.28 倍，刚好看得见）
C_LINE_HI = "#29445c"     # 高亮描边 / 焦点框
C_GLOW = "#0e3a52"        # 光晕底

# ---- 文字层（四档，全部中性偏冷；蓝通道刻意压低，避免在蓝底上“发虚”）
C_TXT = "#eaedf3"         # L1 主文字 / 数值       对比 15.2（B-R=9，仅留极轻冷调）
C_TXT_DIM = "#b8c0cb"     # L2 正文 / 表格内容     对比  9.7
C_MUTED = "#8d96a4"       # L3 次要 / 标签         对比  6.0
C_FAINT = "#66707e"       # L4 纯装饰（角标/刻度/版本号），对比 3.6，不放正文

# ---- 强调层（同族两色，一个亮一个沉）
C_ACC = "#35d2f0"         # 强调青：描边、发光、主按钮底
C_ACC_D = "#1aa8c8"       # 强调青（按下）
C_ACC_TXT = "#6fdcf2"     # 强调青（只用于文字，比 C_ACC 柔和，长时间看不刺）
C_ACC2 = "#5478e8"        # 电蓝：只做渐变尾 / 次强调

# ---- 状态色（全部降到中饱和，不再用纯霓虹）
C_OK = "#3ecfa0"          # 绿：已退出 / 可以操作
C_WARN = "#d9a24e"        # 琥珀：注意事项
C_BAD = "#e8758a"         # 粉红：禁止 / 失败（比纯红柔和，深底上不刺眼）

# ---- 导航 / 顶栏
C_NAV_BG = "#080e18"      # 左侧导航底
C_NAV_ON = "#122b40"      # 导航选中底
C_HEAD_BG = "#060a12"     # 顶部标题条（近黑）
C_HEAD_BG2 = "#0b1a2b"    # 顶部渐变尾色
C_HEAD_TXT = "#e8f1fc"
C_HEAD_MUTED = "#7a8da6"

# ---- 表格
C_SEL = "#123a52"         # 选中行
C_ROW_A = "#0e1826"       # 偶数行
C_ROW_B = "#16222f"       # 奇数行（对比 1.22，条纹清楚但不脏）

# ---- 警示条（暗红底 + 柔粉字，替代原来的纯亮红字）
C_WARN_BG = "#2a1018"
C_WARN_TXT = "#ffb3c0"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

HOME = os.path.expanduser("~")
WB_DIR = os.path.join(HOME, ".workbuddy")
DB_PATH = os.path.join(WB_DIR, "workbuddy.db")
PROJECTS_DIR = os.path.join(WB_DIR, "projects")
TOOL_DIR = _app_dir()          # exe 模式下 = exe 所在目录（不是解包临时目录）
CONFIG_PATH = os.path.join(TOOL_DIR, "tool-config.json")
LOG_PATH = os.path.join(TOOL_DIR, "迁移日志.txt")
README_PATH = os.path.join(TOOL_DIR, "使用说明.txt")
BACKUP_ROOT = os.path.join(TOOL_DIR, "数据库备份")

UID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

# ---------------------------------------------------------------- 自动登录（登录态快照）
# 【v2.0 定论 · 2026-10-26】真正的“我是谁”是产品级凭证文件，不是 Cookie：
#
#   %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
#     ├ auth.accessToken / auth.refreshToken —— 加密信封 {$wbEncrypted:1, envelope}
#     │    设备级密钥在 ~/.workbuddy/keyblob（slots[0].type = "static-v1"），
#     │    同一台机器上用同一把密钥，所以整份文件直接拷来拷去即可，
#     │    不需要解密也不需要重新加密。
#     ├ account.uid / account.uin / account.type —— 当前账号身份（明文）
#     ├ accounts / allAccounts —— 账号列表（每个账号登录过就有一条）
#     └ auth.expiresAt / refreshExpiresAt —— 令牌有效期（毫秒）
#   asar 实证：这段路径在 productCredentials 规则里（FILE_ACTION_ASK），
#   登出时写 <同名>.logged-out 标记，标记存在则该 .info 被忽略。
#
#   ★ 换号 = 整份覆盖这个 .info（+ account-snapshot.json），重启即换人。
#   ★ v1.2/v1.3 只换 Cookie / Chromium leveldb / account-snapshot 之所以
#     永远登回旧账号：客户端启动时读的就是 auth 里的 token，读完还会把
#     account-snapshot、leveldb、Cookie 一起改回旧账号——表现就是“检测
#     一直是前一个账号”。现在以 auth 文件为第一优先级。
SNAPSHOT_ROOT = os.path.join(TOOL_DIR, "登录态快照")
WB_APP_DIR = os.path.join(WB_DIR, "app")
COOKIES_PATH = os.path.join(WB_APP_DIR, "session", "Network", "Cookies")
LOCAL_STORAGE_DIR = os.path.join(WB_DIR, "local_storage")
CHROMIUM_LS_DIR = os.path.join(WB_APP_DIR, "session", "Local Storage")
CHROMIUM_SS_DIR = os.path.join(WB_APP_DIR, "session", "Session Storage")
# 快照里 Chromium 存储的存档子目录名
SNAP_SUB_LS = "chromium_local_storage"
SNAP_SUB_SS = "chromium_session_storage"
# ★ “当前账号”指针文件：asar 里 getSnapshotFilePath() =
#   ~/.workbuddy/storage/skeleton/account-snapshot.json，FirstScreen 启动时
#   读它决定登录哪个账号（ACCOUNT_SNAPSHOT_PERSIST_CHANNEL 实时持久化）。
ACCOUNT_SNAPSHOT_PATH = os.path.join(WB_DIR, "storage", "skeleton", "account-snapshot.json")

# ★★★ 产品级凭证目录（决定登录态本体）。多候选，取第一个真实存在的。
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expandvars(r"%LOCALAPPDATA%")
AUTH_DIR_CANDIDATES = [
    os.path.join(LOCALAPPDATA, "CodeBuddyExtension", "Data", "Public", "auth"),
    os.path.join(LOCALAPPDATA, "WorkBuddyExtension", "Data", "Public", "auth"),
    os.path.join(os.path.expanduser("~"), "AppData", "Local",
                 "CodeBuddyExtension", "Data", "Public", "auth"),
]
SNAP_SUB_AUTH = "auth"          # 快照里放 auth 文件的子目录名
LOGOUT_MARK_SUFFIX = ".logged-out"


# ---------------------------------------------------------------- 基础小函数

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_ts(ms):
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)


def fmt_size(n):
    if n is None or n < 0:
        return "底稿缺失"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 1024 / 1024)


def short_uid(uid):
    return (uid or "")[:8]


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def write_log_line(msg):
    line = "[%s] %s" % (now_str(), msg)
    try:
        with open(LOG_PATH, "a", encoding="gbk", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 环境探测

# ★ v3.06：所有子进程一律“隐形”。
#   正式版 exe 是 --windowed（无控制台）程序，一旦调 tasklist / taskkill /
#   powershell，Windows 会给每个子进程各开一个黑色控制台窗口。退出 WorkBuddy
#   时每 0.8 秒轮询一次 → 满屏黑框（v3.05 实测就是这个现象）。
_CREATE_NO_WINDOW = 0x08000000


def _hidden_kw():
    """给 subprocess 用的“不弹黑框”参数。"""
    kw = {}
    if os.name == "nt":
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                      # SW_HIDE
            kw["startupinfo"] = si
            kw["creationflags"] = _CREATE_NO_WINDOW
        except Exception:
            kw = {}
    return kw


def run_hidden(cmd, timeout=15, encoding=None):
    """跑一条控制台命令，全程不弹黑框。成功返回 CompletedProcess，异常返回 None。"""
    kw = _hidden_kw()
    if encoding:
        kw["encoding"] = encoding
        kw["errors"] = "replace"
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    except Exception:
        return None


def _proc_exists(name):
    """用 Toolhelp 进程快照判断某 exe 是否在跑。

    纯 ctypes，不开子进程 → 不会弹黑框，也不占 CPU（毫秒级）。
    返回 True/False；探测失败返回 None，调用方回退 tasklist。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == 2 ** 64 - 1:
            return None
        try:
            want = name.lower()
            ent = PROCESSENTRY32W()
            ent.dwSize = ctypes.sizeof(ent)
            ok = k32.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                if (ent.szExeFile or "").lower() == want:
                    return True
                ok = k32.Process32NextW(snap, ctypes.byref(ent))
            return False
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return None


def app_running():
    """WorkBuddy 是否还在跑（有任一进程算在跑）。"""
    fast = _proc_exists("WorkBuddy.exe")
    if fast is not None:
        return fast
    out = run_hidden(
        ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
        timeout=15, encoding="gbk")
    if out is None:
        return False
    return "WorkBuddy.exe" in (out.stdout or "")


def quit_app_gently():
    """温柔退出 WorkBuddy（不带 /F，让应用自己收尾）。"""
    return run_hidden(["taskkill", "/IM", "WorkBuddy.exe"], timeout=15) is not None


def crashpad_uid():
    """只走进程探针：WB crashpad 子进程命令行里的 uid（本次启动登的账号）。
    兼容 \\"uid\\":\\" 与 "uid":" 两种格式；探不到返回空串。"""
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'WorkBuddy.exe\'" '
              '| ForEach-Object { $_.CommandLine }')
        out = run_hidden(["powershell", "-NoProfile", "-NoLogo", "-Command", ps],
                         timeout=25, encoding="utf-8")
        found = re.findall(r'\\*"uid\\*"\s*:\s*\\*"([0-9a-fA-F-]{36})', out.stdout or "")
        if found:
            best, bestn = found[0], 0
            for u in set(found):
                n = found.count(u)
                if n > bestn:
                    best, bestn = u, n
            return best
    except Exception:
        pass
    return ""


def current_uid():
    """当前登录的账号 uid。探针按可靠性排序：

    0) ★ 产品级凭证文件 auth\\*.info 的 account.uid —— 登录态本体，客户端
       启动时读它决定“我是谁”。v2.0 起作为第一优先级（之前缺这一环，
       所以换了 Cookie/leveldb 也永远显示前一个账号）。
    1) storage/skeleton/account-snapshot.json 的 primary.uid——UI 层身份指针，
       WB 启动/切换账号时经 account-snapshot:persist 实时重写；正常与 0) 一致。
    2) crashpad 子进程命令行里的 uid（进程启动时写死，反映“本次启动的账号”）。
       注意 WB 5.9.2 起 URL 参数是 JSON 转义格式（\\"uid\\":\\"<uuid>\\"），
       正则必须同时兼容转义和非转义两种写法。
    3) local_storage/wb_entry_*.info 里各账号条目的 savedAt（毫秒）。
    4) 哪个账号的 <uid>/startup-route.json 的 savedAt 最新。
    """
    # --- 探针 0：凭证文件（登录态本体，最权威）
    got = auth_uid()
    if got:
        return got
    # --- 探针 1：账号指针文件（UI 层缓存，与 0) 通常一致）
    ptr = read_account_snapshot()
    uid = (ptr or {}).get("primary", {}).get("uid", "")
    if uid and UID_RE.match(uid):
        return uid
    # --- 探针 2：crashpad URL（兼容 \"uid\":\" 与 "uid":" 两种格式）
    got = crashpad_uid()
    if got:
        return got
    # --- 探针 2：wb_entry_*.info 的 savedAt 最新者（切换账号实时更新）
    try:
        best, best_ts = "", 0
        for name in os.listdir(LOCAL_STORAGE_DIR):
            if not name.endswith(".info"):
                continue
            try:
                with open(os.path.join(LOCAL_STORAGE_DIR, name), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for el in items:
                if not isinstance(el, dict):
                    continue
                key = str(el.get("key", ""))
                uid = key.split("|", 1)[0] if "|" in key else ""
                if not UID_RE.match(uid):
                    continue
                ts = el.get("savedAt", 0) or 0
                if ts > best_ts:
                    best, best_ts = uid, ts
        if best:
            return best
    except Exception:
        pass
    # --- 探针 3：startup-route.json 的 savedAt 最新者
    best, best_ts = "", 0
    try:
        for name in os.listdir(WB_DIR):
            if not UID_RE.match(name):
                continue
            p = os.path.join(WB_DIR, name, "startup-route.json")
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ts = json.load(f).get("savedAt", 0) or 0
                if ts > best_ts:
                    best, best_ts = name, ts
            except Exception:
                pass
    except Exception:
        pass
    return best


_dir_cache = {}


def project_dir_for(cwd):
    r"""由 sessions.cwd 反推 ~/.workbuddy/projects/ 下的目录名。
    规则：盘符小写 + 去掉冒号 + / \ 换成 -。对不上再模糊匹配兜底。"""
    if not cwd:
        return ""
    if cwd in _dir_cache:
        return _dir_cache[cwd]
    cand = cwd[0].lower() + cwd[1:].replace(":", "").replace("/", "-").replace("\\", "-")
    p = os.path.join(PROJECTS_DIR, cand)
    if os.path.isdir(p):
        _dir_cache[cwd] = p
        return p
    best = ""
    try:
        key = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", cwd).lower()
        if os.path.isdir(PROJECTS_DIR):
            for name in os.listdir(PROJECTS_DIR):
                nk = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", name).lower()
                if nk == key:
                    best = os.path.join(PROJECTS_DIR, name)
                    break
    except Exception:
        pass
    _dir_cache[cwd] = best
    return best


def ro_connect():
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- 数据读取

def load_accounts():
    """本机所有账号：来源 = sessions.user_id ∪ user-* 个人目录 ∪ <uid> 目录。"""
    acc = {}

    def touch(uid):
        if uid and UID_RE.match(uid):
            return acc.setdefault(uid, {"uid": uid, "sessions": 0, "last": 0})
        return None

    try:
        con = ro_connect()
        for r in con.execute(
                "SELECT user_id, COUNT(*) c, MAX(COALESCE(last_activity_at, updated_at)) mx "
                "FROM sessions GROUP BY user_id"):
            a = touch(r["user_id"])
            if a:
                a["sessions"] = r["c"]
                a["last"] = r["mx"] or 0
        con.close()
    except Exception:
        pass
    try:
        for name in os.listdir(WB_DIR):
            m = re.match(r"^user-([0-9a-fA-F-]{36})-.+$", name)
            if m:
                touch(m.group(1))
            elif UID_RE.match(name):
                touch(name)
    except Exception:
        pass
    # 昵称
    cfg = load_config()
    nicks = cfg.get("nicks", {})
    cur = current_uid()
    for uid, a in acc.items():
        a["nick"] = nicks.get(uid, "")
        a["is_current"] = (uid == cur)
    return acc


def load_sessions():
    out = []
    try:
        con = ro_connect()
        for r in con.execute(
                "SELECT id, user_id, cwd, title, custom_title, status, deleted_at, "
                "created_at, updated_at, last_activity_at, transport, source_mode "
                "FROM sessions ORDER BY COALESCE(last_activity_at, updated_at) DESC"):
            d = dict(r)
            d["title"] = d.get("custom_title") or d.get("title") or "(无标题)"
            pj = project_dir_for(d.get("cwd") or "")
            jl = os.path.join(pj, d["id"] + ".jsonl") if pj else ""
            d["has_file"] = bool(jl and os.path.exists(jl))
            d["size"] = os.path.getsize(jl) if d["has_file"] else -1
            out.append(d)
        con.close()
    except Exception:
        pass
    return out


def load_automations():
    out = []
    try:
        con = ro_connect()
        for r in con.execute(
                "SELECT id, name, owner_user_id, status, schedule_type, rrule, "
                "scheduled_at, next_run_at, deleted_at, updated_at "
                "FROM automations ORDER BY updated_at DESC"):
            out.append(dict(r))
        con.close()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 备份 / 写库

def backup_db(tag):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_ROOT, "%s-%s" % (tag, ts))
    os.makedirs(dst, exist_ok=True)
    n = 0
    for suf in ("", "-wal", "-shm"):
        src = DB_PATH + suf
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, os.path.basename(src)))
            n += 1
    # 只保留最近 10 份备份
    try:
        olds = sorted((os.path.join(BACKUP_ROOT, x) for x in os.listdir(BACKUP_ROOT)),
                      key=os.path.getmtime)
        while len(olds) > 10:
            shutil.rmtree(olds.pop(0), ignore_errors=True)
    except Exception:
        pass
    return dst, n


def do_move(ids, target, log):
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA busy_timeout=20000")
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        for sid in ids:
            cur.execute("UPDATE sessions SET user_id=? WHERE id=?", (target, sid))
        con.commit()
    finally:
        con.close()
    log("已把 %d 个会话的归属改为 %s" % (len(ids), short_uid(target)))


def do_copy(ids, target, log):
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA busy_timeout=20000")
        cur = con.cursor()
        cols = [c[0] for c in cur.execute("SELECT * FROM sessions LIMIT 0").description]
        cur.execute("BEGIN IMMEDIATE")
        for sid in ids:
            row = cur.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                continue
            d = dict(zip(cols, row))
            old_id = d["id"]
            new_id = str(uuid.uuid4())
            d["id"] = new_id
            d["user_id"] = target
            t = (d.get("title") or "").strip()
            if "（互通副本）" not in t:
                d["title"] = (t + "（互通副本）") if t else "（互通副本）"
            d["custom_title"] = None
            d["unread"] = 0
            # 底稿拷贝（把文件里出现的旧会话 ID 换成新 ID）
            pj = project_dir_for(d.get("cwd") or "")
            if pj:
                for suf in (".jsonl", ".file-rollback.ndjson"):
                    src = os.path.join(pj, old_id + suf)
                    if os.path.exists(src):
                        with open(src, "rb") as f:
                            data = f.read()
                        data = data.replace(old_id.encode("ascii"), new_id.encode("ascii"))
                        with open(os.path.join(pj, new_id + suf), "wb") as f:
                            f.write(data)
                srcdir = os.path.join(pj, old_id)
                if os.path.isdir(srcdir):
                    shutil.copytree(srcdir, os.path.join(pj, new_id))
            names = list(d.keys())
            cur.execute("INSERT INTO sessions (%s) VALUES (%s)" %
                        (",".join(names), ",".join("?" * len(names))),
                        [d[n] for n in names])
            u = cur.execute("SELECT used,size,updated_at,credit_json "
                            "FROM session_usage WHERE session_id=?", (old_id,)).fetchone()
            if u:
                cur.execute("INSERT OR REPLACE INTO session_usage "
                            "(session_id,used,size,updated_at,credit_json) "
                            "VALUES (?,?,?,?,?)", (new_id,) + tuple(u))
            log("复制 %s → 新会话 %s（%s）" %
                (short_uid(old_id), short_uid(new_id), (d.get("title") or "")[:40]))
        con.commit()
    finally:
        con.close()
    log("复制完成：%d 个会话已分身给 %s" % (len(ids), short_uid(target)))


def do_move_automation(ids, target, log):
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA busy_timeout=20000")
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        for aid in ids:
            cur.execute("UPDATE automations SET owner_user_id=? WHERE id=?", (target, aid))
        con.commit()
    finally:
        con.close()
    log("已把 %d 个定时任务的归属改为 %s" % (len(ids), short_uid(target)))


# ---------------------------------------------------------------- 自动登录：快照保存/回装/启动

def find_wb_exe():
    """定位 WorkBuddy.exe：①运行中进程 ②注册表 App Paths ③常见安装路径。"""
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'WorkBuddy.exe\'" '
              '| Select-Object -First 1 -ExpandProperty ExecutablePath')
        out = run_hidden(["powershell", "-NoProfile", "-NoLogo", "-Command", ps],
                         timeout=25, encoding="utf-8")
        first = (out.stdout or "").strip().splitlines() if out else []
        if first and os.path.exists(first[0].strip()):
            return first[0].strip()
    except Exception:
        pass
    try:
        import winreg
        for root in (winreg.HKLM, winreg.HKCU):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\App Paths\WorkBuddy.exe") as k:
                    p, _ = winreg.QueryValueEx(k, None)
                    if p and os.path.exists(p):
                        return p
            except Exception:
                pass
    except Exception:
        pass
    for p in (r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
              r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe")):
        if os.path.exists(p):
            return p
    return ""


def snapshot_dir_for(uid):
    return os.path.join(SNAPSHOT_ROOT, uid)


# ---------------------------------------------------------------- 凭证文件（auth）探测

def find_auth_dir():
    """定位产品凭证目录（真正的登录态文件所在）。找不到返回空串。"""
    for d in AUTH_DIR_CANDIDATES:
        if os.path.isdir(d):
            return d
    return ""


def _auth_names(auth_dir):
    """auth 目录里属于登录态的文件名：*.info 及其附属（观测文件、登出标记）。"""
    out = []
    try:
        for n in os.listdir(auth_dir):
            if ".info" in n or n.endswith(LOGOUT_MARK_SUFFIX):
                out.append(n)
    except Exception:
        pass
    return out


def find_auth_info(auth_dir=None):
    """返回主凭证 .info 的完整路径（找不到返回空串）。

    auth 目录里除了主文件，还可能有：
      workbuddy-desktop.info.<hash>.observation.json  （附属观测文件，sessionId）
      workbuddy-desktop.info.logged-out               （登出标记，存在则被忽略）
      带时间戳的历史登录文件
    主文件 = 名字最短的那个 *.info。
    """
    d = auth_dir or find_auth_dir()
    if not d:
        return ""
    best = ""
    for n in _auth_names(d):
        if not n.endswith(".info"):
            continue
        if not best or len(n) < len(best):
            best = n
    return os.path.join(d, best) if best else ""


def read_auth_identity(info_path=None):
    """读凭证文件里的身份信息（只读，不解密任何字段）。

    返回 dict：path/uid/uin/type/hasToken/hasRefresh/expiresAt/
    refreshExpiresAt/expired/accountCount/error 以及用于“是否被客户端改写”
    的指纹字段 sessionState/lastRefreshTime/mtime/loggedOut。
    """
    p = info_path or find_auth_info()
    out = {"path": p, "uid": "", "uin": "", "type": "", "hasToken": False,
           "hasRefresh": False, "expiresAt": 0, "refreshExpiresAt": 0,
           "expired": False, "accountCount": 0, "error": "",
           "sessionState": "", "lastRefreshTime": 0, "mtime": 0,
           "loggedOut": False, "size": 0}
    if not p or not os.path.exists(p):
        out["error"] = "找不到凭证文件"
        out["loggedOut"] = bool(p) and os.path.exists(p + LOGOUT_MARK_SUFFIX)
        return out
    try:
        st = os.stat(p)
        out["mtime"] = st.st_mtime
        out["size"] = st.st_size
    except Exception:
        pass
    out["loggedOut"] = os.path.exists(p + LOGOUT_MARK_SUFFIX)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as ex:
        out["error"] = "读不了凭证文件：%s" % ex
        return out
    if not isinstance(d, dict):
        out["error"] = "凭证文件格式异常"
        return out
    acct = d.get("account") or {}
    auth = d.get("auth") or {}
    if not isinstance(acct, dict):
        acct = {}
    if not isinstance(auth, dict):
        auth = {}
    out["uid"] = str(acct.get("uid") or "")
    out["uin"] = str(acct.get("uin") or "")
    out["type"] = str(acct.get("type") or "")
    out["hasToken"] = bool(auth.get("accessToken"))
    out["hasRefresh"] = bool(auth.get("refreshToken"))
    out["sessionState"] = str(auth.get("sessionState") or "")
    try:
        out["expiresAt"] = int(auth.get("expiresAt") or 0)
        out["refreshExpiresAt"] = int(auth.get("refreshExpiresAt") or 0)
        out["lastRefreshTime"] = int(auth.get("lastRefreshTime") or 0)
    except Exception:
        pass
    now_ms = int(time.time() * 1000)
    out["expired"] = bool(out["expiresAt"] and out["expiresAt"] < now_ms)
    al = d.get("allAccounts") or d.get("accounts") or []
    out["accountCount"] = len(al) if isinstance(al, list) else 0
    if not UID_RE.match(out["uid"]):
        out["error"] = "凭证文件里没有有效的 account.uid"
    return out


def auth_fingerprint():
    """凭证文件指纹：客户端一旦读写过它，这些值就会变（用于启动后判定成败）。"""
    i = read_auth_identity()
    return {"uid": i["uid"], "mtime": i["mtime"], "size": i["size"],
            "session": i["sessionState"], "refresh": i["lastRefreshTime"],
            "expired": i["expired"], "loggedOut": i["loggedOut"]}


def has_logout_marker(info_path=None):
    """凭证文件旁边是否有 .logged-out 标记（有则客户端忽略该文件）。"""
    p = info_path or find_auth_info()
    return bool(p) and os.path.exists(p + LOGOUT_MARK_SUFFIX)


def auth_uid():
    """凭证文件里的当前账号 uid（探针 0，最权威）；读不到返回空串。"""
    i = read_auth_identity()
    return i["uid"] if UID_RE.match(i["uid"] or "") else ""


def runtime_uid():
    """运行期的“真实”当前账号，用于手动切换后确认。

    ★ 先读凭证文件：用户手动登录后客户端会立刻把新账号写进去，所以它
      最能反映“现在是谁”。凭证旁边有登出标记就别信它（说明已登出）。
    ★ 拿不到才退回进程探针（crashpad 要跑 PowerShell，慢，别每次都用）。
    """
    i = read_auth_identity()
    if not i["loggedOut"] and UID_RE.match(i["uid"] or ""):
        return i["uid"]
    return crashpad_uid()



def read_account_snapshot(path=None):
    """读 account-snapshot.json，返回 dict 或 None。"""
    try:
        with open(path or ACCOUNT_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def write_account_snapshot(uid, nick, path=None, log=None):
    """把“当前账号”写成指定 uid。用于：①保存快照时校准
    （强杀后文件可能还停在旧账号）②回装旧版快照时现场构造。"""
    path = path or ACCOUNT_SNAPSHOT_PATH
    old = read_account_snapshot(path)
    data = dict(old or {})
    primary = dict(data.get("primary") or {})
    primary["version"] = 1
    primary["uid"] = uid
    if nick:
        primary["nickname"] = nick
    primary.setdefault("type", "personal")
    primary["savedAt"] = int(time.time() * 1000)
    data["primary"] = primary
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)  # 原子替换，防 WB 读到半截
        if log:
            log("账号指针已写入：%s（%s）" % (nick or short_uid(uid), short_uid(uid)))
        return True
    except Exception as ex:
        if log:
            log("写账号指针失败：%s" % ex)
        return False


# 同一时刻只允许一个线程写快照（自动捕获线程 vs 用户手动点保存）
_SNAPSHOT_WRITE_LOCK = threading.Lock()
# 快照目录里的临时/备份目录前缀（都以 _ 开头，扫描快照时会被忽略）
WORK_PREFIX = "_tmp-"
OLD_PREFIX = "_old-"


def _rmtree_force(path, tries=5):
    """删目录：先去掉只读位，遇到瞬时占用就重试。

    Chromium 的 leveldb 文件刚被读过时，Windows 上偶尔会短暂占用，
    rename 之后立刻删会失败（实测留了个 _old-* 残骸）。这里重试几次就干净了。
    返回 True 表示目录确实不在了。
    """
    if not os.path.isdir(path):
        return True

    def _onexc(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    for i in range(max(1, tries)):
        try:
            try:
                shutil.rmtree(path, onexc=_onexc)        # Python 3.12+
            except TypeError:
                shutil.rmtree(path, onerror=_onexc)      # Python 3.11 及更早
        except Exception:
            pass
        if not os.path.isdir(path):
            return True
        time.sleep(0.15 * (i + 1))
    return not os.path.isdir(path)


def _prepare_work_dir(uid, final_d):
    """在快照根目录下准备一个临时工作目录，并把旧快照整份继承进来。

    ★ 为什么要继承：部分保存（WB 运行中）只写凭证，靠的就是“旧目录里的
      Cookie/leveldb 还在”。所以新快照必须在旧快照的副本上改，不能凭空建。
    """
    os.makedirs(SNAPSHOT_ROOT, exist_ok=True)
    # 先清掉本账号以前遗留的临时/备份目录（上次保存崩在中间、或删除时被占用而留下的）
    try:
        for name in os.listdir(SNAPSHOT_ROOT):
            if name.startswith(WORK_PREFIX + uid) or name.startswith(OLD_PREFIX + uid):
                _rmtree_force(os.path.join(SNAPSHOT_ROOT, name))
    except Exception:
        pass
    work = os.path.join(SNAPSHOT_ROOT, "%s%s-%d-%s" % (
        WORK_PREFIX, uid, os.getpid(), os.urandom(3).hex()))
    if os.path.isdir(final_d):
        shutil.copytree(final_d, work)
    else:
        os.makedirs(work)
    return work


def _commit_work_dir(work, final_d):
    """把临时目录整体换成正式目录。同卷 rename 是原子的，失败自动回滚。

    以前是“就地删掉旧的再拷新的”，崩在中间那个账号的快照就半残了；
    现在旧快照在改名那一刻之前一直完好。
    """
    old = None
    if os.path.isdir(final_d):
        old = os.path.join(SNAPSHOT_ROOT, "%s%s-%d" % (
            OLD_PREFIX, os.path.basename(final_d), os.getpid()))
        _rmtree_force(old)
        os.rename(final_d, old)
    try:
        os.rename(work, final_d)
    except Exception:
        # 换名失败 → 把旧目录改回去，就当这次没发生
        if old and not os.path.isdir(final_d):
            try:
                os.rename(old, final_d)
            except Exception:
                pass
        raise
    if old:
        # 删不掉也无所谓（数据已经换好了），下次保存会再清一次
        _rmtree_force(old)


def save_login_snapshot(uid, nick, log, allow_running=False):
    """把“当前登录态”存成快照。

    ★ v2.0 核心：产品级凭证文件 auth\\*.info（accessToken/refreshToken 本体）。
      Cookie / Chromium 存储只是辅助层（让界面状态也一致），有就一起存。
    ★ 传入的 uid 只作提示：真值以凭证文件里的 account.uid 为准，不一致时
      自动校准（强杀后 account-snapshot.json 可能还停在旧账号）。
    ★ allow_running=True 供「自动捕获」用：WB 在跑也照存，但只存凭证+指针
      （Cookie / leveldb 被应用锁着，跳过并记 partial=True）。凭证本身才是
      决定“我是谁”的东西，所以 partial 快照照样能切换登录。
    """
    running = app_running()
    if running and not allow_running:
        raise RuntimeError("WorkBuddy 还在运行，请先退出再保存登录态"
                           "（凭证文件和 Cookie 都被应用锁着，运行中拷出来会不完整）")
    partial = bool(running and allow_running)
    info = read_auth_identity()
    if not UID_RE.match(info["uid"]):
        raise RuntimeError(
            "读不到有效的登录凭证。\n\n"
            "凭证文件：%s\n原因：%s\n\n"
            "请确认 WorkBuddy 已完全退出后重试；若一直读不到，说明这台机器上\n"
            "该账号的登录态已经不在，重新登录一次即可。"
            % (info["path"] or "（未找到 auth 目录）", info["error"] or "未知"))
    real_uid = info["uid"]
    if real_uid != uid:
        log("注意：凭证文件里的账号是 %s，与传入的 %s 不一致 → 按凭证文件校准"
            % (short_uid(real_uid), short_uid(uid)))
        uid = real_uid
        try:
            ptr_nick = (read_account_snapshot() or {}).get("primary", {}).get("nickname", "")
            nick = ptr_nick or nick
        except Exception:
            pass
    final_d = snapshot_dir_for(uid)
    # ★ 原子写：所有内容先落到临时目录 work，全部成功后再整体换名成正式目录。
    #   以前是就地删了再拷，崩在中间那个账号的快照就半残；现在旧快照在换名
    #   那一刻之前一直完好，异常时直接丢弃 work 即可。
    with _SNAPSHOT_WRITE_LOCK:
        work = _prepare_work_dir(uid, final_d)
        d = work                      # 下面所有写入都落在临时目录
        committed = False
        try:

            # ★ 第一步（核心）：凭证文件整份存档
            auth_d = find_auth_dir()
            dst_auth = os.path.join(d, SNAP_SUB_AUTH)
            if os.path.isdir(dst_auth):
                shutil.rmtree(dst_auth, ignore_errors=True)
            auth_saved = 0
            if auth_d:
                os.makedirs(dst_auth, exist_ok=True)
                for name in _auth_names(auth_d):
                    src = os.path.join(auth_d, name)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(dst_auth, name))
                        auth_saved += 1
            if auth_saved == 0:
                raise RuntimeError("auth 目录里没找到可存档的登录凭证：%s" % (auth_d or "（目录不存在）"))

            if partial:
                log("（WorkBuddy 正在运行，本次只存登录凭证，Cookie/浏览器存储跳过）")
            else:
                # --- 辅助层：Cookie
                for suf in ("", "-journal"):
                    src = COOKIES_PATH + suf
                    if os.path.exists(src):
                        shutil.copy2(src, os.path.join(d, "Cookies" + suf))
                # --- 辅助层：按账号分条的请求缓存
                dst_ls = os.path.join(d, "local_storage")
                if os.path.isdir(dst_ls):
                    shutil.rmtree(dst_ls, ignore_errors=True)
                if os.path.isdir(LOCAL_STORAGE_DIR):
                    shutil.copytree(LOCAL_STORAGE_DIR, dst_ls)
                # --- 辅助层：Chromium Local/Session Storage（整目录，leveldb 需配套）
                for src_dir, sub in ((CHROMIUM_LS_DIR, SNAP_SUB_LS), (CHROMIUM_SS_DIR, SNAP_SUB_SS)):
                    dst = os.path.join(d, sub)
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    if os.path.isdir(src_dir):
                        shutil.copytree(src_dir, dst)

            # --- 身份指针：按凭证文件 uid 现写一份再存档（强杀后它可能停在旧账号）。
            #     运行中不写（会被客户端覆盖），直接拷现有那份，但先确认它确实是本账号。
            if partial:
                ptr = read_account_snapshot() or {}
                if (ptr.get("primary") or {}).get("uid") == uid and os.path.exists(ACCOUNT_SNAPSHOT_PATH):
                    shutil.copy2(ACCOUNT_SNAPSHOT_PATH, os.path.join(d, "account-snapshot.json"))
            else:
                write_account_snapshot(uid, nick, log=log)
                if os.path.exists(ACCOUNT_SNAPSHOT_PATH):
                    shutil.copy2(ACCOUNT_SNAPSHOT_PATH, os.path.join(d, "account-snapshot.json"))

            # 本次若是“运行中只存凭证”，但快照里原本已有 Cookie/存储辅助层，
            # 那这份快照整体仍算完整（辅助层是同一账号早先存的，照样能用）。
            has_aux = (os.path.exists(os.path.join(d, "Cookies"))
                       or os.path.isdir(os.path.join(d, SNAP_SUB_LS)))
            meta = {"format": 2,
                    "uid": uid,
                    "nick": nick,
                    "uin": info["uin"],
                    "savedAt": now_str(),
                    "authFile": os.path.basename(info["path"]),
                    "authFiles": auth_saved,
                    "sessionState": info["sessionState"],
                    "lastRefreshTime": info["lastRefreshTime"],
                    "tokenExpiresAt": info["expiresAt"],
                    "refreshExpiresAt": info["refreshExpiresAt"],
                    "tokenExpired": info["expired"],
                    "partial": bool(partial and not has_aux)}
            with open(os.path.join(d, "快照信息.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)

            # ★ 全部写完，才把临时目录换成正式目录（同卷 rename 原子）
            _commit_work_dir(work, final_d)
            committed = True
            d = final_d
            log("已保存登录态快照：%s（%s）→ 凭证 %d 个文件%s"
                % (nick or short_uid(uid), short_uid(uid), auth_saved,
                   "（仅凭证）" if partial else " + Cookie/存储辅助层"))
            if info["expired"]:
                log("提醒：该账号的 accessToken 已过期（%s），启动时会自动用 refreshToken 续期"
                    % fmt_ts(info["expiresAt"]))
            if info["refreshExpiresAt"] and info["refreshExpiresAt"] < int(time.time() * 1000):
                log("警告：该账号的 refreshToken 也已过期（%s），切换前需重新登录一次"
                    % fmt_ts(info["refreshExpiresAt"]))
            if info["loggedOut"]:
                log("警告：凭证旁边存在登出标记（.logged-out），这份登录态可能已失效；"
                    "若切换失败请重新登录一次再保存")
            return d
        finally:
            if not committed:
                _rmtree_force(work)


def load_snapshots():
    """扫描快照目录 → {uid: {uid,nick,savedAt,dir,format,hasAuth}}，忽略 _ 开头的备份目录。

    hasAuth=True 表示快照里有凭证文件（v2 格式，能真正换号）；
    False = 旧格式，需要重新保存一次。
    """
    out = {}
    try:
        for name in os.listdir(SNAPSHOT_ROOT):
            if name.startswith("_") or not UID_RE.match(name):
                continue
            d = os.path.join(SNAPSHOT_ROOT, name)
            if not os.path.isdir(d):
                continue
            meta = {}
            try:
                with open(os.path.join(d, "快照信息.json"), "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass
            au = os.path.join(d, SNAP_SUB_AUTH)
            has_auth = os.path.isdir(au) and bool(_auth_names(au))
            out[name] = {"uid": name,
                         "nick": meta.get("nick", ""),
                         "savedAt": meta.get("savedAt", ""),
                         "dir": d,
                         "format": meta.get("format", 1),
                         "uin": meta.get("uin", ""),
                         "sessionState": meta.get("sessionState", ""),
                         "lastRefreshTime": meta.get("lastRefreshTime", 0),
                         "tokenExpiresAt": meta.get("tokenExpiresAt", 0),
                         "refreshExpiresAt": meta.get("refreshExpiresAt", 0),
                         "partial": bool(meta.get("partial")),
                         "hasAuth": has_auth}
    except Exception:
        pass
    return out


def restore_login_snapshot(uid, nick, log):
    """回装指定账号的登录态（自动先把当前登录态备份到 _回装前-时间戳）。

    ★ v2.0 执行顺序（auth 在前是硬要求）：
      ① 备份当前凭证目录 + 辅助层
      ② 清掉当前凭证目录里的登录态文件 → 拷入快照的凭证（含登出标记清理）
      ③ 回读校验凭证里的 uid 就是目标账号
      ④ 再回装 account-snapshot（身份指针）与 Cookie/Chromium 辅助层
    """
    if app_running():
        raise RuntimeError("WorkBuddy 还在运行，请先退出")
    d = snapshot_dir_for(uid)
    src_auth = os.path.join(d, SNAP_SUB_AUTH)
    if not os.path.isdir(src_auth) or not _auth_names(src_auth):
        if os.path.isdir(d):
            raise RuntimeError(
                "【%s】的快照是旧格式（不含登录凭证），已失效。\n\n"
                "旧方案只存了 Cookie 和浏览器存储，缺了真正决定“我是谁”的凭证\n"
                "文件，所以切过去也会登回原账号。请这样重建：\n\n"
                "1) 在 WorkBuddy 里登录该账号\n"
                "2) 回到本工具点「保存当前登录态」\n"
                "3) 每个要切换的账号都做一遍\n\n"
                "（重建一次以后就一直能用）" % (nick or short_uid(uid)))
        raise RuntimeError("没有 %s 的登录态快照，请先「保存当前登录态」"
                           % (nick or short_uid(uid)))

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(SNAPSHOT_ROOT, "_回装前-%s" % ts)
    n = 2
    while os.path.exists(bak):  # 同一秒内连续切换 → 名字加后缀防撞车
        bak = os.path.join(SNAPSHOT_ROOT, "_回装前-%s-%d" % (ts, n))
        n += 1
    os.makedirs(bak, exist_ok=True)

    # --- ① 备份当前：凭证目录（核心）+ Cookie + 存储 + 指针
    auth_d = find_auth_dir()
    if auth_d:
        dst_bak_auth = os.path.join(bak, SNAP_SUB_AUTH)
        os.makedirs(dst_bak_auth, exist_ok=True)
        for name in _auth_names(auth_d):
            src = os.path.join(auth_d, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dst_bak_auth, name))
    for suf in ("", "-journal"):
        if os.path.exists(COOKIES_PATH + suf):
            shutil.copy2(COOKIES_PATH + suf, os.path.join(bak, "Cookies" + suf))
    if os.path.isdir(LOCAL_STORAGE_DIR):
        shutil.copytree(LOCAL_STORAGE_DIR, os.path.join(bak, "local_storage"),
                        dirs_exist_ok=True)
    for src_dir, sub in ((CHROMIUM_LS_DIR, SNAP_SUB_LS), (CHROMIUM_SS_DIR, SNAP_SUB_SS)):
        if os.path.isdir(src_dir):
            shutil.copytree(src_dir, os.path.join(bak, sub), dirs_exist_ok=True)
    if os.path.exists(ACCOUNT_SNAPSHOT_PATH):
        shutil.copy2(ACCOUNT_SNAPSHOT_PATH, os.path.join(bak, "account-snapshot.json"))

    # --- ② 回装凭证：先清掉当前目录里的登录态文件，再整份拷入
    if not auth_d:
        raise RuntimeError("找不到凭证目录，无法回装：\n%s"
                           % "\n".join(AUTH_DIR_CANDIDATES))
    for name in _auth_names(auth_d):
        try:
            os.remove(os.path.join(auth_d, name))
        except Exception:
            pass
    for name in _auth_names(src_auth):
        s = os.path.join(src_auth, name)
        if not os.path.isfile(s):
            continue
        # 快照里若带登出标记就跳过（有它在客户端会忽略凭证）
        if name.endswith(LOGOUT_MARK_SUFFIX):
            log("跳过登出标记：%s" % name)
            continue
        shutil.copy2(s, os.path.join(auth_d, name))

    # --- ③ 校验凭证：uid 必须就是目标账号
    got_info = read_auth_identity()
    if got_info["uid"] != uid:
        log("警告：回装的凭证 uid=%s 与目标 %s 不一致"
            % (short_uid(got_info["uid"]) or "空", short_uid(uid)))
    else:
        log("凭证已回装并校验通过：%s（token %s）"
            % (short_uid(uid), "已过期，将自动续期" if got_info["expired"] else "有效"))

    # --- ④ 辅助层：Cookie
    for suf in ("", "-journal"):
        src = os.path.join(d, "Cookies" + suf)
        if os.path.exists(src):
            shutil.copy2(src, COOKIES_PATH + suf)
    src_ls = os.path.join(d, "local_storage")
    if os.path.isdir(src_ls) and os.path.isdir(LOCAL_STORAGE_DIR):
        for name in os.listdir(src_ls):
            s = os.path.join(src_ls, name)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(LOCAL_STORAGE_DIR, name))
    # Chromium 存储：先删目标目录再整目录拷入（LOCK/CURRENT/MANIFEST 必须配套）
    for dst_dir, sub in ((CHROMIUM_LS_DIR, SNAP_SUB_LS), (CHROMIUM_SS_DIR, SNAP_SUB_SS)):
        src = os.path.join(d, sub)
        if os.path.isdir(src):
            if os.path.isdir(dst_dir):
                shutil.rmtree(dst_dir, ignore_errors=True)
            shutil.copytree(src, dst_dir)
    # --- 身份指针：优先用快照里存的；没有就按 uid 现场写一份
    snap_ptr = os.path.join(d, "account-snapshot.json")
    if os.path.exists(snap_ptr):
        shutil.copy2(snap_ptr, ACCOUNT_SNAPSHOT_PATH)
    else:
        write_account_snapshot(uid, nick, log=log)
    # 指针里的 uid 必须也是目标账号
    ptr = read_account_snapshot()
    ptr_uid = (ptr or {}).get("primary", {}).get("uid", "")
    if ptr_uid and ptr_uid != uid:
        write_account_snapshot(uid, nick, log=log)
        log("警告：账号指针与目标不符（%s），已按目标账号重写" % short_uid(ptr_uid))
    log("已回装 %s 的登录态（凭证 + 指针 + Cookie/存储，原登录态已备份到 %s）"
        % (nick or short_uid(uid), bak))
    return bak


def launch_workbuddy(log):
    exe = find_wb_exe()
    if not exe:
        raise RuntimeError("找不到 WorkBuddy.exe，请手动打开")
    # DETACHED_PROCESS(0x8)：让 WB 不继承本工具的控制台。
    # 否则用户关掉“双击启动.bat”弹出的黑窗口会连带把 WB 一起收走
    # （同 console 的进程会被系统一起终止，2026-10-25 实测踩坑）。
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         creationflags=0x00000008)
    except Exception:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    log("已启动 WorkBuddy：%s" % exe)
    return exe


def quit_and_wait(timeout=45):
    """温柔退出 WorkBuddy 并等到真正退出（再等一会落盘）。返回 True=已退出。

    注意：WorkBuddy 关闭窗口后会缩到系统托盘继续后台运行
    （app.asar 里有 closeToTray 逻辑），所以光发“关闭窗口”不够，
    本函数先试优雅 taskkill；退不掉时由上层弹引导窗教用户
    “右键托盘图标 → 退出”，或用 force_quit_wb() 兜底。"""
    quit_app_gently()
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not app_running():
            time.sleep(1.5)  # 给 Cookie 等文件一点落盘时间
            return True
        time.sleep(0.8)
    return not app_running()


def force_quit_wb(log=None):
    """强制退出 WorkBuddy（taskkill /F）。不到万不得已不要用：
    可能丢未落盘的库改动。调用前应已备份数据库。"""
    try:
        out = run_hidden(["taskkill", "/F", "/IM", "WorkBuddy.exe"],
                         timeout=20, encoding="gbk")
        if out is None:
            if log:
                log("强制退出失败：taskkill 没能启动")
            return False
        ok = out.returncode == 0
        if log:
            log("已强制退出 WorkBuddy（%s）" % (out.stdout or out.stderr or "").strip()[:80])
        return ok
    except Exception as ex:
        if log:
            log("强制退出失败：%s" % ex)
        return False


# ---------------------------------------------------------------- 主界面

class App(object):
    def __init__(self, root):
        self.root = root
        self.root.title("%s %s" % (APP_TITLE, APP_VER))
        # 居中显示（按屏幕尺寸算，避免每次都在左上角）
        try:
            w, h = 1200, 880
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(w, sw - 60)
            h = min(h, sh - 80)
            self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                                max(0, (sh - h) // 2 - 24)))
        except Exception:
            self.root.geometry("1180x860")
        self.root.minsize(1000, 700)
        try:
            self.root.iconbitmap(default=app_icon_path())
        except Exception:
            pass

        self.cfg = load_config()
        self.accounts = {}
        self.sessions = []
        self.automations = []
        self.snapshots = {}
        self.sel = set()                 # 选中的会话 id
        self.sel_auto = set()            # 选中的定时任务 id
        self.tree_items = {}
        self.auto_items = {}
        self.mode = tk.StringVar(value="move")
        # 用量统计结果（后台线程填）
        self.usage = None
        self._usage_busy = False
        self._usage_new = False
        self._usage_error = ""
        self._ucache = None
        # 自动捕获登录态：见 _auto_watch_loop
        self._closing = False
        self._suppress_until = 0.0
        self._auto_busy = False

        self._build_ui()
        self._load_log_tail()
        self.refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        threading.Thread(target=self._auto_watch_loop, daemon=True).start()

    # ---------------- 自动捕获登录态

    def _on_window_close(self):
        self._closing = True
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------- 无边框窗口控件（系统标题栏已去掉，全自己来）

    def win_min(self):
        """最小化。overrideredirect 下 Tk 的 iconify 会报错（override-redirect
        flag is set），所以直接叫 Windows 收起来；任务栏按钮还在，点一下就回来。"""
        try:
            from ctypes import windll
            hwnd = win_hwnd(self.root)
            if hwnd:
                windll.user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
        except Exception:
            pass

    def win_toggle_max(self):
        """最大化 / 还原。自己按工作区算，避免盖住任务栏。"""
        try:
            if getattr(self, "_maxed", False):
                self._maxed = False
                g = getattr(self, "_restore_geom", "")
                if g:
                    self.root.geometry(g)
            else:
                self._restore_geom = self.root.wm_geometry()
                self._maxed = True
                x, y, w, h = work_area(self.root)
                self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            try:
                self.header.set_maxed(self._maxed)
            except Exception:
                pass
            self._layout_grips()
        except Exception:
            pass

    def win_close(self):
        """关闭按钮 = 退出本工具（不是退 WorkBuddy）。"""
        self._on_window_close()

    # ---- 拖窗口：顶栏空白处按住拖
    def _drag_press(self, e):
        if self.header.zone_at(e) or getattr(self, "_maxed", False):
            return
        self._drag0 = (e.x_root, e.y_root,
                       self.root.winfo_rootx(), self.root.winfo_rooty())

    def _drag_move(self, e):
        d = getattr(self, "_drag0", None)
        if not d:
            return
        self.root.geometry("+%d+%d" % (d[2] + e.x_root - d[0],
                                       d[3] + e.y_root - d[1]))

    def _drag_end(self, e=None):
        self._drag0 = None

    def _drag_dbl(self, e):
        """顶栏空白处双击 = 最大化/还原（跟系统标题栏一致）。"""
        if not self.header.zone_at(e):
            self.win_toggle_max()

    # ---- 缩放：四边 + 四角热区（系统边框没了，只能自己接）
    def _layout_grips(self):
        maxed = getattr(self, "_maxed", False)
        for key, f in getattr(self, "_grips", {}).items():
            if maxed:
                f.place_forget()
            else:
                f.place(**self._grip_specs[key])
                f.lift()

    def _grip_press(self, e, edge):
        self._rz = (edge, e.x_root, e.y_root, self.root.winfo_rootx(),
                    self.root.winfo_rooty(), self.root.winfo_width(),
                    self.root.winfo_height())
        return "break"

    def _grip_move(self, e):
        r = getattr(self, "_rz", None)
        if not r:
            return "break"
        edge, sx, sy, x0, y0, w0, h0 = r
        dx, dy = e.x_root - sx, e.y_root - sy
        mw, mh = self.root.minsize()
        mw = mw or 1000
        mh = mh or 700
        x, y, w, h = x0, y0, w0, h0
        if "e" in edge:
            w = max(mw, w0 + dx)
        if "w" in edge:
            w = max(mw, w0 - dx)
            x = x0 + (w0 - w)
        if "s" in edge:
            h = max(mh, h0 + dy)
        if "n" in edge:
            h = max(mh, h0 - dy)
            y = y0 + (h0 - h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        return "break"

    def _grip_end(self, e=None):
        self._rz = None

    def _suppress_auto(self, seconds=90):
        """刚做完保存/切换，短暂关掉自动捕获，避免把刚回装的凭证又抓一遍。"""
        self._suppress_until = time.time() + seconds

    def _auto_watch_loop(self):
        """后台盯着凭证文件：只要有新账号登录（或同账号重新登录），自动存档。

        这样就不必“每个账号各登录一次再手动点保存”了 —— 登录即入库。
        （同类的 WorkDaddy 是挂 CDP 监听登录网络事件；我们直接看凭证文件，
        更简单，也不用给客户端加启动参数。）

        去重：只在 (uid, sessionState) 变了才动手；刷新 token 不改 sessionState。
        抑制：保存/切换刚做完的 90 秒内不抓，避免自我循环。
        """
        while not self._closing:
            try:
                time.sleep(4.0)
                if self._closing:
                    break
                if self._auto_busy or time.time() < self._suppress_until:
                    continue
                info = read_auth_identity()
                if not UID_RE.match(info["uid"]) or info["loggedOut"]:
                    continue
                cur = load_snapshots().get(info["uid"], {})
                need = False
                # ① 这个账号还没存过，或换了登录会话（重新登录过）
                if info["sessionState"] and cur.get("sessionState") != info["sessionState"]:
                    need = True
                # ② 客户端续期过 token（lastRefreshTime 变新）→ 顺手把快照里的
                #    凭证也刷新，免得快照里的 token 越来越旧
                if (info["lastRefreshTime"]
                        and info["lastRefreshTime"] > (cur.get("lastRefreshTime") or 0)):
                    need = True
                if not need:
                    continue
                self._auto_busy = True
                try:
                    nick = (self.accounts.get(info["uid"], {}).get("nick", "")
                            or (read_account_snapshot() or {}).get("primary", {}).get("nickname", ""))
                    self.log("检测到登录态：%s（%s），自动存档……"
                             % (nick or "未命名", short_uid(info["uid"])))
                    save_login_snapshot(info["uid"], nick, self.log, allow_running=True)
                    self.root.after(0, self.refresh_launch)
                except Exception as ex:
                    msg = str(ex)
                    self.log("自动存档失败：%s" % msg)
                finally:
                    self._auto_busy = False
            except Exception:
                continue

    # ---------------- 日志

    def _load_log_tail(self, n=200):
        """启动时把 迁移日志.txt 的尾部读进日志框，免得一进来是空白。"""
        try:
            with open(LOG_PATH, "r", encoding="gbk", errors="replace") as f:
                lines = f.readlines()[-n:]
            if lines:
                self.logbox.insert(tk.END, "".join(lines))
                self.logbox.insert(tk.END, "\n—— 以上为历史记录，以下为本次操作 ——\n")
                self.logbox.see(tk.END)
        except Exception:
            pass

    def log(self, msg):
        write_log_line(msg)

        def _do():
            try:
                self.logbox.insert(tk.END, "[%s] %s\n" % (now_str()[11:19], msg))
                self.logbox.see(tk.END)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    # ---------------- 界面骨架

    def _paint_page_bg(self, frame):
        """页面底纹：顶部渐隐 + 稀疏点阵（HUD 网格感）。

        放在页面最底层（lower()），pack/place 上去的控件自然盖住它，
        只有空白处会露出来 —— 正好解决深色主题下大片纯黑显得“空”的问题。
        """
        cv = tk.Canvas(frame, bg=C_PAGE, highlightthickness=0, bd=0)
        cv.place(x=0, y=0, relwidth=1, relheight=1)
        try:
            # 注意：cv.lower() 是 Canvas 的"图形项下沉"，不是窗口层叠，
            # 这里要的是窗口叠放顺序（Tk 的 lower <window>）。
            cv.tk.call("lower", cv._w)
        except Exception:
            pass

        def _draw(_e=None):
            try:
                cv.delete("all")
                w = cv.winfo_width() or 900
                h = cv.winfo_height() or 700
                _vgrad(cv, 0, 0, w, 64, "#0c1622", C_PAGE, steps=32)
                for x in range(12, w, 34):
                    for y in range(12, h, 34):
                        cv.create_rectangle(x, y, x + 1, y + 1,
                                            fill="#111b27", outline="")
                cv.create_line(0, 0, 0, h, fill="#0c1723")
            except Exception:
                pass
        cv.bind("<Configure>", _draw)
        return cv

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---------------- 顶部：HUD 应用条（Canvas 自绘，含流光、呼吸灯、窗口控件）
        self.header = TechHeader(self.root, APP_TITLE, APP_VER,
                                 on_refresh=self.refresh_all,
                                 on_quit=self.on_quit_app,
                                 on_min=self.win_min,
                                 on_max=self.win_toggle_max,
                                 on_close=self.win_close)
        self.header.pack(fill=tk.X)
        # 顶栏空白处按住 = 拖窗口；双击 = 最大化/还原
        self.header.cv.bind("<ButtonPress-1>", self._drag_press, add="+")
        self.header.cv.bind("<B1-Motion>", self._drag_move, add="+")
        self.header.cv.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self.header.cv.bind("<Double-Button-1>", self._drag_dbl, add="+")

        # 提示条（红色警示，平时隐藏）
        self.warn_var = tk.StringVar(value="")
        self.warn_lbl = tk.Label(self.root, textvariable=self.warn_var, bg=C_WARN_BG,
                                 fg=C_WARN_TXT, anchor="w", justify="left",
                                 font=(FONT_UI, 9), padx=16, pady=2)
        self.warn_lbl.pack(fill=tk.X)
        # 长警示文案按窗口宽度换行（窄窗口下原来会被裁掉半句）
        # 注意：子控件的 <Configure> 会冒泡到 toplevel 的绑定上，必须过滤 e.widget
        def _fit_warn(e):
            if e.widget is not self.root:
                return
            try:
                self.warn_lbl.configure(wraplength=max(560, e.width - 40))
            except Exception:
                pass
        self.root.bind("<Configure>", _fit_warn, add="+")

        # ---------------- 主体：左侧导航 + 右侧分页
        body = tk.Frame(self.root, bg=C_PAGE)
        body.pack(fill=tk.BOTH, expand=True, padx=(0, 0), pady=(0, 0))
        nav_items = (("account", "◉", "账号", "账号一览 / 一键切换 / 导入导出"),
                     ("usage", "▤", "用量统计", "积分·Token·限流·活跃度"),
                     ("session", "▦", "会话迁移", "会话与定时任务过户"),
                     ("log", "≡", "日志", "操作记录"))
        self.nav = TechNav(body, nav_items, on_pick=self.show_page,
                           on_help=self.show_help, on_dir=self.open_tool_dir)
        self.nav.pack(side=tk.LEFT, fill=tk.Y)
        self.content = tk.Frame(body, bg=C_PAGE)
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.pages = {}
        for key, icon, label, tip in nav_items:
            f = tk.Frame(self.content, bg=C_PAGE)
            f.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages[key] = f
            self._paint_page_bg(f)

        pg_acc = self.pages["account"]
        pg_ses = self.pages["session"]
        pg_log = self.pages["log"]

        # ---- 账号页：概览卡片（Canvas 玻璃卡）
        cardrow = tk.Frame(pg_acc, bg=C_PAGE)
        cardrow.pack(fill=tk.X, padx=10, pady=(10, 2))
        self.card_vars = {}
        for i, (key, title, hint, acc) in enumerate((
                ("cur", "当前登录账号", "凭证文件", C_ACC),
                ("snap", "可切换账号", "登录态快照", C_ACC2),
                ("credit", "本月积分消耗", "本地记录", "#4fa8f5"),
                ("active", "活跃度", "对话记录", C_OK))):
            card = TechCard(cardrow, title, hint, acc)
            card.pack(side=tk.LEFT, fill=tk.X, expand=True,
                      padx=(0 if i == 0 else 8, 0))
            self.card_vars[key] = card.var

        # 账号面板
        af = ttk.LabelFrame(pg_acc, text=" 账号（双击行可改昵称） ")
        af.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        cols = ("nick", "uid", "sess", "credit", "last", "snap", "cur")
        self.acc_tree = ttk.Treeview(af, columns=cols, show="headings", height=6)
        for c, t, w, a in (("nick", "昵称", 140, "w"), ("uid", "账号 ID", 210, "w"),
                           ("sess", "会话数", 62, "center"), ("credit", "本月积分", 82, "center"),
                           ("last", "最近活动", 132, "center"), ("snap", "登录态快照", 96, "center"),
                           ("cur", "当前", 48, "center")):
            self.acc_tree.heading(c, text=t, anchor=a)
            self.acc_tree.column(c, width=w, anchor=a, stretch=(c == "uid"))
        self.acc_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.acc_tree.bind("<Double-1>", self.on_nick_edit)
        sb = ttk.Scrollbar(af, orient=tk.VERTICAL, command=self.acc_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.acc_tree.configure(yscrollcommand=sb.set)

        # 建议条（积分/限流时给切换建议）
        # ★ v3.05：原来是“换整块底色”（琥珀/暗红/深蓝），字色固定灰蓝 →
        #   暖底配冷字最显脏。改成底色恒定，只用左侧一条 3px 状态色竖条表意。
        self.advice_var = tk.StringVar(value="")
        self.advice_wrap = tk.Frame(pg_acc, bg=C_CARD,
                                    highlightbackground=C_LINE, highlightthickness=1)
        self.advice_bar = tk.Frame(self.advice_wrap, bg=C_ACC, width=3)
        self.advice_bar.pack(side=tk.LEFT, fill=tk.Y)
        self.advice_bar.pack_propagate(False)
        self.advice_lbl = tk.Label(self.advice_wrap, textvariable=self.advice_var,
                                   bg=C_CARD, fg=C_TXT_DIM, anchor="w", justify="left",
                                   wraplength=980, padx=12, pady=7,
                                   font=("Microsoft YaHei UI", 9))
        self.advice_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.advice_wrap.pack(fill=tk.X, padx=8, pady=(2, 4))

        # 会话面板
        sf = ttk.LabelFrame(pg_ses, text=" 会话（勾选要迁移/复制的任务） ")
        sf.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        bar = ttk.Frame(sf)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="刷新", command=self.refresh_all,
                   style="Subtle.TButton").pack(side=tk.LEFT)
        ttk.Label(bar, text="  只看账号：").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value="全部")
        self.filter_cb = ttk.Combobox(bar, textvariable=self.filter_var, width=26, state="readonly")
        self.filter_cb.pack(side=tk.LEFT)
        self.filter_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_sessions())
        ttk.Label(bar, text="  搜标题：").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=18)
        ent.pack(side=tk.LEFT)
        ent.bind("<KeyRelease>", lambda e: self.refresh_sessions())
        ttk.Button(bar, text="全选", command=lambda: self.select_all(True),
                   style="Subtle.TButton").pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(bar, text="全不选", command=lambda: self.select_all(False),
                   style="Subtle.TButton").pack(side=tk.LEFT, padx=(6, 0))

        cols = ("title", "owner", "cwd", "upd", "size", "trans", "status")
        self.sess_tree = ttk.Treeview(sf, columns=cols, show="tree headings", height=9)
        self.sess_tree.heading("#0", text="✓")
        self.sess_tree.column("#0", width=34, anchor="center", stretch=False)
        for c, t, w in (("title", "标题", 290), ("owner", "所属账号", 120),
                        ("cwd", "文件夹", 226), ("upd", "更新时间", 120),
                        ("size", "底稿大小", 78), ("trans", "传输", 54), ("status", "状态", 62)):
            self.sess_tree.heading(c, text=t)
            self.sess_tree.column(c, width=w, stretch=(c in ("title", "cwd")),
                                  anchor="w" if c in ("title", "cwd") else "center")
        self.sess_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2 = ttk.Scrollbar(sf, orient=tk.VERTICAL, command=self.sess_tree.yview)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)
        self.sess_tree.configure(yscrollcommand=sb2.set)
        self.sess_tree.bind("<Button-1>", self.on_sess_click)
        self.sess_tree.tag_configure("cloud", foreground=C_WARN)
        self.sess_tree.tag_configure("deleted", foreground=C_MUTED)
        self.sess_tree.tag_configure("nofile", foreground=C_BAD)

        # 操作面板
        of = ttk.LabelFrame(pg_ses, text=" 迁移 / 复制 ")
        of.pack(fill=tk.X, padx=8, pady=4)
        row = ttk.Frame(of)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="目标账号：").pack(side=tk.LEFT)
        self.target_var = tk.StringVar()
        self.target_cb = ttk.Combobox(row, textvariable=self.target_var, width=30, state="readonly")
        self.target_cb.pack(side=tk.LEFT)
        ttk.Radiobutton(row, text="迁移（过继给目标账号，原账号不再显示）", variable=self.mode,
                        value="move").pack(side=tk.LEFT, padx=(24, 0))
        ttk.Radiobutton(row, text="复制（目标账号多一个分身，两边都能续）", variable=self.mode,
                        value="copy").pack(side=tk.LEFT, padx=(14, 0))
        self.btn_go = ttk.Button(row, text="开始执行", command=self.on_go, style="Accent.TButton")
        self.btn_go.pack(side=tk.RIGHT)
        ttk.Label(of, text="提示：想把 B 账号的会话给 A，就把“只看账号”选 B、目标选 A，反之亦然。"
                          "云端会话（传输=cloud）跟随服务器账号，不能本地迁移。",
                  foreground=C_MUTED).pack(fill=tk.X, **pad)

        # 定时任务面板
        tf = ttk.LabelFrame(pg_ses, text=" 定时任务 / 自动化（勾选后过户 owner） ")
        tf.pack(fill=tk.X, padx=8, pady=4)
        tbar = ttk.Frame(tf)
        tbar.pack(fill=tk.X)
        ttk.Button(tbar, text="迁移勾选的定时任务归属 → 目标账号", command=self.on_go_auto).pack(side=tk.LEFT)
        ttk.Label(tbar, text="  （会话可以迁，定时任务也可以；不过户的话切换账号后就看不到这条监测了）",
                  foreground=C_MUTED).pack(side=tk.LEFT)
        acols = ("name", "owner", "status", "sched", "next")
        self.auto_tree = ttk.Treeview(tf, columns=acols, show="tree headings", height=3)
        self.auto_tree.heading("#0", text="✓")
        self.auto_tree.column("#0", width=34, anchor="center")
        for c, t, w in (("name", "名称", 300), ("owner", "当前归属", 130),
                        ("status", "状态", 80), ("sched", "周期", 220), ("next", "下次运行", 150)):
            self.auto_tree.heading(c, text=t)
            self.auto_tree.column(c, width=w, anchor="w" if c in ("name", "sched") else "center")
        self.auto_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb3 = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.auto_tree.yview)
        sb3.pack(side=tk.RIGHT, fill=tk.Y)
        self.auto_tree.configure(yscrollcommand=sb3.set)
        self.auto_tree.bind("<Button-1>", self.on_auto_click)

        # 启动账号（自动登录）+ 账号备份（合并成一个面板，省纵向空间）
        kf = ttk.LabelFrame(pg_acc, text=" 启动账号（自动登录：选谁，启动就是谁） · 账号备份 ")
        kf.pack(fill=tk.X, padx=8, pady=(4, 6))
        krow = ttk.Frame(kf)
        krow.pack(fill=tk.X, **pad)
        ttk.Label(krow, text="启动账号：").pack(side=tk.LEFT)
        self.launch_var = tk.StringVar()
        self.launch_cb = ttk.Combobox(krow, textvariable=self.launch_var,
                                      width=30, state="readonly")
        self.launch_cb.pack(side=tk.LEFT)
        self.btn_save_snap = ttk.Button(krow, text="保存当前登录态", command=self.on_save_snapshot)
        self.btn_save_snap.pack(side=tk.LEFT, padx=(12, 0))
        self.btn_launch = ttk.Button(krow, text="切换并启动", command=self.on_launch_as,
                                     style="Accent.TButton")
        self.btn_launch.pack(side=tk.LEFT, padx=6)
        ttk.Label(krow, text="｜", foreground="#2a4a63").pack(side=tk.LEFT, padx=6)
        ttk.Button(krow, text="导入备份…", command=self.on_import_accounts).pack(side=tk.LEFT)
        ttk.Button(krow, text="加密导出…", command=self.on_export_accounts).pack(side=tk.LEFT, padx=6)
        self.snap_var = tk.StringVar(value="（暂无快照）")
        ttk.Label(kf, textvariable=self.snap_var, foreground=C_MUTED,
                  font=(FONT_UI, 8)).pack(fill=tk.X, **pad)
        self.exp_var = tk.StringVar(
            value="快照 = 该账号的登录凭证（token 本体），在 WorkBuddy 里登录任意账号会自动存档。"
                  "「加密导出」把全部账号打包成口令加密的 .wbacc，换机/重装可一键导入。")
        ttk.Label(kf, textvariable=self.exp_var, foreground=C_MUTED,
                  font=(FONT_UI, 8), wraplength=1000,
                  justify="left").pack(fill=tk.X, **pad)

        # 用量统计页
        self._build_usage_page(self.pages["usage"])

        # 日志
        lf = ttk.LabelFrame(pg_log, text=" 操作日志 ")
        lf.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        # ★ v3.05：原来是“纯黑底 + 浅青绿字”，在玻璃面板上是一块突兀的黑板；
        #   改成和表格同底的玻璃面 + 上升一档的灰青字，并加行距。
        self.logbox = ScrolledText(lf, height=7, wrap=tk.WORD, font=(FONT_MONO, 9),
                                   bg=C_CARD, fg="#9db2c4", relief="flat",
                                   insertbackground=C_TXT, padx=10, pady=8,
                                   spacing1=2, spacing3=3,
                                   highlightthickness=1, highlightbackground=C_LINE,
                                   highlightcolor=C_LINE, bd=0)
        self.logbox.pack(fill=tk.BOTH, expand=True)
        # 日志里的强调行：原来的亮红改成柔红，跟色板一致
        self.logbox.tag_configure("err", foreground=C_BAD)
        self.logbox.tag_configure("ok", foreground=C_OK)
        self.logbox.tag_configure("hi", foreground=C_ACC_TXT)

        # 底部状态栏
        tk.Frame(self.root, bg=C_LINE, height=1).pack(fill=tk.X, side=tk.BOTTOM)
        bot = tk.Frame(self.root, bg=C_PAGE)
        bot.pack(fill=tk.X, side=tk.BOTTOM, padx=14, pady=6)
        self.nav_tip = tk.Label(bot, text="", bg=C_PAGE, fg=C_MUTED, font=(FONT_UI, 8))
        self.nav_tip.pack(side=tk.LEFT)
        tk.Label(bot, text="备份：数据库备份\\　日志：迁移日志.txt",
                 bg=C_PAGE, fg=C_MUTED, font=(FONT_UI, 8)).pack(side=tk.RIGHT)

        # ---------------- 无边框窗口：四边 / 四角拖拽缩放热区
        # 系统边框已经去掉，缩放只能自己接：这几条细边颜色故意压得很暗，
        # 远看就是一道钢蓝色机身边，近看才是可拖拽的抓手。
        self._grips = {}
        self._grip_specs = {}
        G, K = 5, 11                     # 边宽 / 角宽
        for key, spec, cur in (
                ("n", dict(x=0, y=0, relwidth=1, height=G), "sb_v_double_arrow"),
                ("s", dict(x=0, rely=1.0, y=-G, relwidth=1, height=G), "sb_v_double_arrow"),
                ("w", dict(x=0, y=0, width=G, relheight=1), "sb_h_double_arrow"),
                ("e", dict(relx=1.0, x=-G, y=0, width=G, relheight=1), "sb_h_double_arrow"),
                ("nw", dict(x=0, y=0, width=K, height=K), "size_nw_se"),
                ("ne", dict(relx=1.0, x=-K, y=0, width=K, height=K), "size_ne_sw"),
                ("sw", dict(x=0, rely=1.0, y=-K, width=K, height=K), "size_ne_sw"),
                ("se", dict(relx=1.0, x=-K, rely=1.0, y=-K, width=K, height=K),
                 "size_nw_se")):
            f = tk.Frame(self.root, bg="#0f2336", bd=0, highlightthickness=0)
            try:
                f.configure(cursor=cur)
            except Exception:
                pass
            f.bind("<ButtonPress-1>", lambda e, k=key: self._grip_press(e, k))
            f.bind("<B1-Motion>", self._grip_move)
            f.bind("<ButtonRelease-1>", self._grip_end)
            self._grips[key] = f
            self._grip_specs[key] = spec
        self._layout_grips()

        self.show_page("account")

    # ---------------- 页面切换

    def show_page(self, key):
        f = self.pages.get(key)
        if not f:
            return
        f.tkraise()
        try:
            self.nav.select(key)
        except Exception:
            pass
        tips = {"account": "双击账号行可改昵称　·　「切换并启动」= 选谁就登谁",
                "usage": "数据全部来自本机 database 与聊天底稿，不上传任何内容",
                "session": "迁移 = 过继，原账号不再显示　·　复制 = 两边各一份",
                "log": "全部操作记录同时写入 迁移日志.txt"}
        self.nav_tip.configure(text=tips.get(key, ""))

    # ---------------- 用量统计页

    def _build_usage_page(self, parent):
        pad = {"padx": 8, "pady": 4}

        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, padx=8, pady=(8, 2))
        ttk.Label(bar, text="账号：").pack(side=tk.LEFT)
        self.usage_filter_var = tk.StringVar(value="全部账号")
        self.usage_filter_cb = ttk.Combobox(bar, textvariable=self.usage_filter_var,
                                            width=26, state="readonly",
                                            values=["全部账号"])
        self.usage_filter_cb.pack(side=tk.LEFT)
        self.usage_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._render_usage())
        ttk.Label(bar, text="  范围：").pack(side=tk.LEFT)
        self.usage_range_var = tk.StringVar(value="全部")
        rng = ttk.Combobox(bar, textvariable=self.usage_range_var, width=9, state="readonly",
                           values=["全部", "近 7 天", "近 30 天", "本月"])
        rng.pack(side=tk.LEFT)
        rng.bind("<<ComboboxSelected>>", lambda e: self._render_usage())
        ttk.Button(bar, text="重新统计", command=lambda: self.refresh_usage(force=True)).pack(
            side=tk.LEFT, padx=(10, 0))
        self.usage_state_var = tk.StringVar(value="尚未统计")
        ttk.Label(bar, textvariable=self.usage_state_var, foreground=C_MUTED).pack(side=tk.RIGHT)

        # 摘要（同样是 Canvas 玻璃卡）
        srow = tk.Frame(parent, bg=C_PAGE)
        srow.pack(fill=tk.X, padx=10, pady=(2, 2))
        self.usage_sum_vars = {}
        for key, title, acc in (("credit", "积分消耗", "#4fa8f5"),
                                ("tokens", "Token 用量", C_ACC),
                                ("turns", "对话回合", C_ACC2),
                                ("streak", "连续活跃", C_OK)):
            card = TechCard(srow, title, "", acc)
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            card.var.set("—")
            self.usage_sum_vars[key] = card.var

        # 柱状图
        cf = ttk.LabelFrame(parent, text=" 每日积分消耗 ")
        cf.pack(fill=tk.X, padx=8, pady=4)
        self.chart = tk.Canvas(cf, height=178, bg=C_CARD, highlightthickness=0)
        self.chart.pack(fill=tk.X, expand=True, padx=6, pady=6)
        self.chart.bind("<Configure>", lambda e: self._render_usage())

        # 排行表（账号 / 模型）
        tf = ttk.Frame(parent)
        tf.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        lf = ttk.LabelFrame(tf, text=" 账号用量排行 ")
        lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.rank_acc = ttk.Treeview(lf, columns=("nick", "credit", "tok", "turns"),
                                     show="headings", height=6)
        for c, t, w in (("nick", "账号", 150), ("credit", "积分", 80),
                        ("tok", "Token", 90), ("turns", "回合", 60)):
            self.rank_acc.heading(c, text=t)
            self.rank_acc.column(c, width=w, anchor="center" if c != "nick" else "w")
        self.rank_acc.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(4, 0), pady=4)
        rf = ttk.LabelFrame(tf, text=" 模型用量排行 ")
        rf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        self.rank_model = ttk.Treeview(rf, columns=("model", "credit", "tok", "turns"),
                                       show="headings", height=6)
        for c, t, w in (("model", "模型", 170), ("credit", "积分", 80),
                        ("tok", "Token", 90), ("turns", "回合", 60)):
            self.rank_model.heading(c, text=t)
            self.rank_model.column(c, width=w, anchor="center" if c != "model" else "w")
        self.rank_model.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(4, 0), pady=4)

        # 限流
        qff = ttk.LabelFrame(parent, text=" 模型频率限制 / 预计解封 ")
        qff.pack(fill=tk.BOTH, expand=False, padx=8, pady=(4, 8))
        self.limit_tree = ttk.Treeview(qff, columns=("model", "acct", "when", "reset", "state"),
                                       show="headings", height=4)
        for c, t, w in (("model", "受限模型", 190), ("acct", "账号", 150),
                        ("when", "最近一次受限", 150), ("reset", "预计解封", 170),
                        ("state", "状态", 110)):
            self.limit_tree.heading(c, text=t)
            self.limit_tree.column(c, width=w, anchor="w" if c in ("model", "acct") else "center")
        self.limit_tree.pack(fill=tk.X, expand=True, padx=4, pady=4)
        self.limit_tree.tag_configure("pending", foreground=C_BAD)
        ttk.Label(qff, text="说明：解封时间取自服务端返回的限流提示（“将在 … 重置”），不是本地估算。",
                  foreground=C_MUTED).pack(fill=tk.X, padx=6, pady=(0, 6))

    def _chart_nice_max(self, v):
        if v <= 0:
            return 10.0
        import math
        mag = 10 ** int(math.floor(math.log10(v)))
        for m in (1, 2, 2.5, 5, 10):
            if v <= mag * m:
                return mag * m
        return mag * 10

    def _draw_chart(self, days):
        """days: [(label, credit, turn_count)]，画成柱状图（Canvas 手绘）。

        科技风要点：柱子做竖直渐变（底部深青 → 顶部亮青）+ 顶部高光条，
        背景用虚线网格，避免浅色表格那种“Excel 味”。
        """
        c = self.chart
        c.delete("all")
        w = c.winfo_width() or 900
        h = int(c.cget("height")) or 178
        pad_l, pad_r, pad_t, pad_b = 48, 12, 16, 28
        if not days:
            c.create_text(w / 2, h / 2, text="暂 无 数 据", fill=C_FAINT,
                          font=(FONT_MONO, 9))
            return
        vmax = self._chart_nice_max(max(d[1] for d in days))
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)
        base_y = pad_t + plot_h
        # 虚线网格 + Y 轴刻度
        for i in range(5):
            y = pad_t + plot_h * i / 4.0
            val = vmax * (4 - i) / 4.0
            c.create_line(pad_l, y, w - pad_r, y, fill="#16273a", dash=(2, 4))
            c.create_text(pad_l - 8, y, text=("%.0f" % val), anchor="e",
                          fill=C_FAINT, font=(FONT_MONO, 7))
        # 基线与左侧轴线
        c.create_line(pad_l, base_y, w - pad_r, base_y, fill="#1e3a52")
        c.create_line(pad_l, pad_t - 6, pad_l, base_y, fill="#16273a")
        n = len(days)
        step = plot_w / float(n)
        bw = max(3, min(30, step * 0.58))
        for i, (label, credit, turns) in enumerate(days):
            cx = pad_l + step * (i + 0.5)
            bh = 0 if vmax <= 0 else plot_h * (credit / vmax)
            if credit > 0 and bh < 2:
                bh = 2
            x0 = cx - bw / 2
            y0 = base_y - bh
            if credit > 0:
                # 竖直渐变：底深青 → 顶亮青
                segs = max(3, min(10, int(bh / 6) + 3))
                for k in range(segs):
                    t = k / float(segs - 1)
                    yy0 = base_y - bh * (k + 1) / segs
                    yy1 = base_y - bh * k / segs
                    col = _mix("#083549", C_ACC, t * 0.95)
                    c.create_rectangle(x0, yy0, x0 + bw, yy1 + 0.5,
                                       fill=col, outline="")
                # 顶部高光
                c.create_line(x0, y0 + 1, x0 + bw, y0 + 1, fill="#9ce4f5", width=2)
                # 柱脚微光
                c.create_line(x0, base_y - 1, x0 + bw, base_y - 1, fill="#0d5e79")
            else:
                c.create_line(x0, base_y - 1, x0 + bw, base_y - 1, fill="#1b2b3a")
            if credit >= vmax * 0.06:
                c.create_text(cx, y0 - 8, text=(" %.0f" % credit).strip(),
                              fill=C_ACC_TXT, font=(FONT_MONO, 7))
            if n <= 26 or i % 2 == 0:
                c.create_text(cx, base_y + 14, text=label, fill=C_FAINT,
                              font=(FONT_MONO, 7))

    def _usage_days_for_chart(self):
        u = self.usage
        if not u:
            return []
        rng = self.usage_range_var.get()
        today = datetime.date.today()
        lim = None
        if rng == "近 7 天":
            lim = today - datetime.timedelta(days=6)
        elif rng == "近 30 天":
            lim = today - datetime.timedelta(days=29)
        elif rng == "本月":
            lim = today.replace(day=1)
        sel = self._usage_selected_uid()

        buckets = {}
        if sel:
            for r in u.get("by_account_day", []):
                if r["uid"] != sel:
                    continue
                buckets[r["day"]] = buckets.get(r["day"], [0.0, 0]) 
                buckets[r["day"]][0] += r["credit"]
                buckets[r["day"]][1] += r["turns"]
        else:
            for r in u.get("by_day", []):
                buckets[r["day"]] = buckets.get(r["day"], [0.0, 0])
                buckets[r["day"]][0] += r["credit"]
                buckets[r["day"]][1] += r["turns"]
        rows = []
        for d, (cr, tn) in sorted(buckets.items()):
            try:
                dd = datetime.date(int(d[:4]), int(d[5:7]), int(d[8:10]))
            except Exception:
                continue
            if lim and dd < lim:
                continue
            rows.append((d[5:], round(cr, 2), tn))
        return rows[-31:]

    def _usage_selected_uid(self):
        val = self.usage_filter_var.get()
        if not val or val.startswith("全部"):
            return ""
        m = re.search(r"（([0-9a-fA-F-]{8})）", val)
        if not m:
            return ""
        head = m.group(1).lower()
        for a in (self.usage or {}).get("by_account", []):
            if a["uid"].lower().startswith(head):
                return a["uid"]
        for uid in self.accounts:
            if uid.lower().startswith(head):
                return uid
        return ""

    def refresh_usage(self, force=False):
        if wb_usage is None:
            self.usage_state_var.set("缺少 wb_usage.py，用量统计不可用")
            return
        if getattr(self, "_usage_busy", False):
            return
        self._usage_busy = True
        self._usage_new = False
        self._usage_error = ""
        self.usage_state_var.set("正在统计…（首次要读聊天底稿，可能要几秒）")
        threading.Thread(target=self._run_collect_usage, daemon=True).start()
        # 主线程轮询：后台线程只写普通属性，不碰任何 Tk 控件
        self.root.after(250, self._usage_poll)

    def _usage_poll(self):
        try:
            if getattr(self, "_usage_busy", False):
                self.root.after(300, self._usage_poll)
                return
            err = getattr(self, "_usage_error", "")
            if err:
                self._usage_error = ""
                self.usage_state_var.set("统计失败：%s" % err)
                return
            if getattr(self, "_usage_new", False):
                self._usage_new = False
                self._render_usage()
        except Exception:
            pass

    def _run_collect_usage(self):
        try:
            cache = self._usage_cache()
            data = wb_usage.collect(cache=cache,
                                    nick_of=lambda u: self.accounts.get(u, {}).get("nick", ""))
            if cache:
                cache.save()
            self.usage = data
            self._usage_new = True
        except Exception as ex:
            self._usage_error = str(ex)
        finally:
            self._usage_busy = False

    def _usage_cache(self):
        if wb_usage is None:
            return None
        if getattr(self, "_ucache", None) is None:
            try:
                p = os.path.join(TOOL_DIR, ".workbuddy", "cache", "usage_turns.json")
                self._ucache = wb_usage.Cache(p)
            except Exception:
                self._ucache = None
        return self._ucache

    def _render_usage(self):
        u = self.usage
        if not u:
            self._draw_chart([])
            return
        sel = self._usage_selected_uid()
        # 下拉项
        opts = ["全部账号"]
        for a in u.get("by_account", []):
            nick = a.get("nick") or self.accounts.get(a["uid"], {}).get("nick") or "未命名"
            opts.append("%s（%s）" % (nick, short_uid(a["uid"])))
        self.usage_filter_cb.configure(values=opts)
        if self.usage_filter_var.get() not in opts:
            self.usage_filter_var.set("全部账号")
            sel = ""

        # 范围过滤后的汇总
        rng = self.usage_range_var.get()
        today = datetime.date.today()
        lim = None
        if rng == "近 7 天":
            lim = today - datetime.timedelta(days=6)
        elif rng == "近 30 天":
            lim = today - datetime.timedelta(days=29)
        elif rng == "本月":
            lim = today.replace(day=1)

        def _in(d):
            if not lim:
                return True
            try:
                return datetime.date(int(d[:4]), int(d[5:7]), int(d[8:10])) >= lim
            except Exception:
                return True

        if sel:
            days = [r for r in u["by_account_day"] if r["uid"] == sel and _in(r["day"])]
        else:
            days = [r for r in u["by_day"] if _in(r["day"])]
        credit = sum(r["credit"] for r in days)
        pin = sum(r["pin"] for r in days)
        pout = sum(r["pout"] for r in days)
        turns = sum(r["turns"] for r in days)
        self.usage_sum_vars["credit"].set(wb_usage.fmt_credit(credit))
        self.usage_sum_vars["tokens"].set("%s / %s" % (wb_usage.fmt_tokens(pin),
                                                       wb_usage.fmt_tokens(pout)))
        self.usage_sum_vars["turns"].set(str(turns))
        self.usage_sum_vars["streak"].set("%d 天%s" % (u["streak"], "　今日活跃" if u["active_today"] else ""))

        self._draw_chart(self._usage_days_for_chart())

        # 账号排行
        for i in self.rank_acc.get_children():
            self.rank_acc.delete(i)
        for a in u.get("by_account", []):
            nick = a.get("nick") or self.accounts.get(a["uid"], {}).get("nick") or "未命名"
            self.rank_acc.insert("", tk.END, values=(
                "%s（%s）" % (nick, short_uid(a["uid"])),
                wb_usage.fmt_credit(a["credit"]),
                wb_usage.fmt_tokens(a["pin"] + a["pout"]),
                a["turns"]))

        # 模型排行（按当前筛选账号聚合）
        for i in self.rank_model.get_children():
            self.rank_model.delete(i)
        if sel:
            rows = [r for r in u.get("by_account_model", []) if r["uid"] == sel]
        else:
            rows = [{"model": m["model"], "credit": m["credit"],
                     "pin": m["pin"], "pout": m["pout"], "turns": m["turns"]}
                    for m in u.get("by_model", [])]
        rows = sorted(rows, key=lambda r: -r["credit"])[:30]
        for r in rows:
            self.rank_model.insert("", tk.END, values=(
                r["model"], wb_usage.fmt_credit(r["credit"]),
                wb_usage.fmt_tokens(r["pin"] + r["pout"]), r["turns"]))

        # 限流
        for i in self.limit_tree.get_children():
            self.limit_tree.delete(i)
        now_ms = int(time.time() * 1000)
        pend = {l["model"]: l for l in u.get("limits_pending_by_model", [])}
        for l in u.get("limits_latest_by_model", []):
            if sel and l["uid"] != sel:
                continue
            p = pend.get(l["model"])
            if p and p["reset_ts"] > now_ms:
                state = "受限中"
                reset = time.strftime("%m-%d %H:%M:%S", time.localtime(p["reset_ts"] / 1000.0))
                tags = ("pending",)
            else:
                state = "已解除"
                reset = (time.strftime("%m-%d %H:%M", time.localtime(l["reset_ts"] / 1000.0))
                         if l.get("reset_ts") else "—")
                tags = ()
            a = self.accounts.get(l["uid"], {})
            nick = a.get("nick") or "未命名"
            self.limit_tree.insert("", tk.END, tags=tags, values=(
                l["model"], "%s（%s）" % (nick, short_uid(l["uid"])),
                time.strftime("%m-%d %H:%M", time.localtime((l["ts"] or 0) / 1000.0)),
                reset, state))

        self.usage_state_var.set("统计完成（%d 个会话，%.1fs）"
                                 % (u.get("sessions_scanned", 0), u.get("elapsed", 0)))
        # 用量页三张表也刷斑马纹（原来只有账号/会话/定时任务表有，这三张是纯色一片）
        for _t in (self.rank_acc, self.rank_model, self.limit_tree):
            self._zebra(_t)
        self.refresh_cards()
        self.refresh_advice()

    # ---------------- 卡片 / 建议

    def refresh_cards(self):
        if not hasattr(self, "card_vars"):
            return
        self._update_credit_column()
        cur = getattr(self, "cur_uid", "") or ""
        nick = self.accounts.get(cur, {}).get("nick", "") if cur else ""
        self.card_vars["cur"].set((nick or "未登录").strip() if cur else "未登录")
        n_ready = sum(1 for s in self.snapshots.values() if s.get("hasAuth"))
        self.card_vars["snap"].set("%d 个" % n_ready)
        u = self.usage or {}
        now = datetime.date.today()
        month_first = now.replace(day=1).strftime("%Y-%m-%d")
        if u.get("by_account_day"):
            if cur:
                mc = sum(r["credit"] for r in u["by_account_day"]
                         if r["uid"] == cur and r["day"] >= month_first)
            else:
                mc = sum(r["credit"] for r in u["by_account_day"] if r["day"] >= month_first)
            self.card_vars["credit"].set(wb_usage.fmt_credit(mc) if wb_usage else "—")
        else:
            self.card_vars["credit"].set("—")
        if u:
            self.card_vars["active"].set("%d 天%s" % (u.get("streak", 0),
                                                     "" if u.get("active_today") else "（今日未聊）"))
        else:
            self.card_vars["active"].set("—")

    def _update_credit_column(self):
        """用量统计好后，只回填账号表的「本月积分」列（不动用户的选择）。"""
        if wb_usage is None or not self.usage:
            return
        month_first = datetime.date.today().replace(day=1).strftime("%Y-%m-%d")
        credit = {}
        for r in self.usage.get("by_account_day", []):
            if r["day"] >= month_first:
                credit[r["uid"]] = credit.get(r["uid"], 0.0) + r["credit"]
        for uid in self.acc_tree.get_children():
            vals = list(self.acc_tree.item(uid, "values"))
            if len(vals) >= 4:
                cr = credit.get(uid)
                vals[3] = wb_usage.fmt_credit(cr) if cr is not None else "0"
                self.acc_tree.item(uid, values=vals)

    def refresh_advice(self):
        """积分/限流视角的“要不要换个账号”建议。"""
        if not hasattr(self, "advice_var"):
            return
        u = self.usage
        if not u:
            self.advice_var.set("正在统计本机用量…（首次统计要读聊天底稿，可能要几秒）")
            self.advice_bar.configure(bg=C_ACC)
            return
        now_ms = int(time.time() * 1000)
        cur = getattr(self, "cur_uid", "") or current_uid()
        pend = [l for l in u.get("limits_pending_by_model", []) if l["reset_ts"] > now_ms]
        # 可切换的备选账号（有凭证、不是当前账号）
        others = [uid for uid, s in self.snapshots.items()
                  if s.get("hasAuth") and uid != cur]
        # 近 7 天各账号消耗
        week_ago = (datetime.date.today() - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
        spend = {}
        for r in u.get("by_account_day", []):
            if r["day"] >= week_ago:
                spend[r["uid"]] = spend.get(r["uid"], 0.0) + r["credit"]
        others.sort(key=lambda x: spend.get(x, 0.0))

        def who(uid):
            return (self.accounts.get(uid, {}).get("nick")
                    or self.snapshots.get(uid, {}).get("nick")
                    or short_uid(uid))

        if pend:
            l = pend[0]
            when = time.strftime("%m-%d %H:%M", time.localtime(l["reset_ts"] / 1000.0))
            msg = "⚠ 模型【%s】正在限流，预计 %s 解封。" % (l["model"], when)
            if others:
                msg += ("若要继续干活，建议切到【%s】（近 7 天消耗最低，且有登录态快照）："
                        "到「账号」页选它 → 「切换并启动」。" % who(others[0]))
                self.advice_bar.configure(bg=C_WARN)
            else:
                msg += "目前没有别的可切换账号（先在 WorkBuddy 里登录另一个账号，工具会自动存档）。"
                self.advice_bar.configure(bg=C_BAD)
        elif spend and cur and spend.get(cur, 0) > 0:
            total = sum(spend.values()) or 1
            share = spend.get(cur, 0) / total
            if share > 0.8 and others:
                self.advice_var.set(
                    "提示：近 7 天消耗的 %.0f%% 都记在当前账号【%s】上（%s 积分）；"
                    "想分担可以把部分任务切到【%s】。"
                    % (share * 100, who(cur), wb_usage.fmt_credit(spend.get(cur, 0)), who(others[0])))
                self.advice_bar.configure(bg=C_ACC)
            else:
                self.advice_var.set(
                    "近 7 天：当前账号 %s 积分，其他账号合计 %s 积分（本地记录，非官方余额）。"
                    % (wb_usage.fmt_credit(spend.get(cur, 0)),
                       wb_usage.fmt_credit(total - spend.get(cur, 0))))
                self.advice_bar.configure(bg=C_LINE_HI)
        else:
            self.advice_var.set("暂无足够的用量记录（多聊几轮后这里会给出切换建议）。")
            self.advice_bar.configure(bg=C_LINE_HI)

    # ---------------- 加密导出 / 导入

    def on_export_accounts(self):
        if wb_crypto is None:
            self._dark_error("缺少模块", ["找不到 wb_crypto.py，无法导出。"])
            return
        uids = [u for u, s in self.snapshots.items() if s.get("hasAuth")]
        if not uids:
            self._dark_info("没有可导出的账号",
                            ["「登录态快照」里还没有带凭证的账号。",
                             "先在 WorkBuddy 里登录一次，工具会自动存档。"])
            return
        default = "账号备份-%s.wbacc" % time.strftime("%Y%m%d")
        path = filedialog.asksaveasfilename(
            title="导出账号备份（加密）", defaultextension=".wbacc",
            initialfile=default, filetypes=[("加密账号备份", "*.wbacc"), ("所有文件", "*.*")])
        if not path:
            return
        pw = simpledialog.askstring("设置口令", "给备份文件设一个口令（至少 4 位）：\n"
                                              "这个口令不会存进文件，丢了就解不开。",
                                    show="*", parent=self.root)
        if not pw:
            return
        if len(pw) < 4:
            self._dark_warn("口令太短", ["请至少输入 4 位。"])
            return
        pw2 = simpledialog.askstring("确认口令", "再输一次同样的口令：", show="*", parent=self.root)
        if pw2 != pw:
            self._dark_error("两次不一致", ["两次输入的口令不一样，请重来。"])
            return
        nicks = {u: (s.get("nick") or self.accounts.get(u, {}).get("nick", ""))
                 for u, s in self.snapshots.items()}
        self.exp_var.set("正在打包加密…（账号越多越慢，几秒钟）")

        def work():
            try:
                info = wb_crypto.export_snapshots(SNAPSHOT_ROOT, uids, path, pw, nicks=nicks)
                msg = "已导出 %d 个账号 →\n%s\n\n文件 %.1f KB（明文 %.1f MB）\n\n口令请自行记牢，丢了无法恢复。"
                self.root.after(0, lambda i=info: (
                    self.exp_var.set("已导出 %d 个账号：%s" % (len(i["accounts"]), i["path"])),
                    self._dark_info("导出完成", [msg % (
                        len(i["accounts"]), i["path"], i["bytes"] / 1024.0,
                        i["plain_bytes"] / 1048576.0)])))
                self.log("已加密导出 %d 个账号的登录态 → %s" % (len(uids), path))
            except Exception as ex:
                m = str(ex)
                self.root.after(0, lambda mm=m: self._dark_error("导出失败", [mm]))
                self.log("导出失败：%s" % m)

        threading.Thread(target=work, daemon=True).start()

    def on_import_accounts(self):
        if wb_crypto is None:
            self._dark_error("缺少模块", ["找不到 wb_crypto.py，无法导入。"])
            return
        path = filedialog.askopenfilename(
            title="选择账号备份文件",
            filetypes=[("加密账号备份", "*.wbacc"), ("所有文件", "*.*")])
        if not path:
            return
        pw = simpledialog.askstring("输入口令", "请输入该备份文件的口令：", show="*", parent=self.root)
        if not pw:
            return
        try:
            man, header = wb_crypto.peek(path, pw)
        except Exception as ex:
            self._dark_error("打不开", [str(ex)])
            return
        accts = man.get("accounts") or []
        lines = []
        for a in accts:
            uid = a.get("uid", "")
            have = os.path.isdir(os.path.join(SNAPSHOT_ROOT, uid))
            lines.append("  · %s（%s）%s%s" % (a.get("nick") or "未命名", short_uid(uid),
                                              "%.1f MB" % (a.get("bytes", 0) / 1048576.0),
                                              "  ← 本机已存在" if have else ""))
        if not lines:
            self._dark_info("空备份", ["这个文件里没有账号数据。"])
            return
        if not self._dark_ask(
                "确认导入",
                ["备份导出时间：%s　来自：%s"
                 % (header.get("created") or "未知", header.get("host") or "未知"),
                 "包含 %d 个账号：" % len(accts)]
                + lines
                + ["", "导入会写入「登录态快照」。同名账号默认跳过，已有更旧快照可选覆盖。",
                   "开始导入吗？"],
                ok_text="导入", cancel_text="取消"):
            return
        overwrite = self._dark_ask(
            "同名账号怎么办",
            ["如果本机已有同名（同账号）快照：", "",
             "覆盖 = 用备份里的盖掉本机的",
             "保留 = 只导入本机没有的"],
            ok_text="覆盖", cancel_text="保留本机")
        self.exp_var.set("正在导入…")

        def work():
            try:
                res, _ = wb_crypto.import_snapshots(path, pw, SNAPSHOT_ROOT, overwrite=overwrite)
                new = sum(1 for r in res if r["action"] == "new")
                ow = sum(1 for r in res if r["action"] == "overwrite")
                skip = sum(1 for r in res if r["action"] == "skip")
                txt = "导入完成：新增 %d 个，覆盖 %d 个，跳过 %d 个。" % (new, ow, skip)
                self.root.after(0, lambda: (
                    self._dark_info("导入完成", [
                        txt,
                        "现在可以在「启动账号」里选它们一键切换。",
                        "注意：登录凭证用本机设备密钥加密，若换机导入后提示需重新登录，"
                        "在该机登录一次即可（工具会自动重新存档）。"]),
                    self.refresh_all()))
                self.log("导入账号备份：%s" % txt)
                self.root.after(0, lambda t=txt: self.exp_var.set(t))
            except Exception as ex:
                m = str(ex)
                self.root.after(0, lambda mm=m: self._dark_error("导入失败", [mm]))
                self.log("导入失败：%s" % m)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 刷新


    def _zebra(self, tree):
        """给表格刷斑马纹（隔行浅底色）。重复调用无副作用。"""
        try:
            tree.tag_configure("odd", background=C_ROW_B)
        except Exception:
            return
        for i, iid in enumerate(tree.get_children()):
            try:
                tags = [t for t in tree.item(iid, "tags") if t != "odd"]
                if i % 2:
                    tags.append("odd")
                tree.item(iid, tags=tags)
            except Exception:
                pass
    def refresh_all(self):
        self.refresh_env()
        self.refresh_launch()          # 先拿快照，账号表要显示快照状态
        self.refresh_accounts()
        self.refresh_sessions()
        self.refresh_automations()
        if self.usage is None:
            self.refresh_usage()       # 首次进界面就在后台统计用量
        else:
            self.refresh_cards()
            self.refresh_advice()

    def refresh_launch(self):
        self.snapshots = load_snapshots()
        now_ms = int(time.time() * 1000)
        # 可切换的排前面，各自按保存时间倒序（避免列表跳动/换位）
        ready = [(u, s) for u, s in self.snapshots.items() if s["hasAuth"]]
        olds = [(u, s) for u, s in self.snapshots.items() if not s["hasAuth"]]
        ready.sort(key=lambda kv: kv[1]["savedAt"] or "", reverse=True)
        olds.sort(key=lambda kv: kv[1]["savedAt"] or "", reverse=True)
        vals = []
        for uid, s in ready + olds:
            nick = s["nick"] or "未命名"
            if not s["hasAuth"]:
                # 区分两种情况：真旧格式（format<2），和“正在存档、auth 目录刚被清空重建”
                # 的瞬时状态——后者别吓唬用户说“需重建”
                if s.get("format", 1) >= 2:
                    vals.append("%s（%s）· 正在更新快照…" % (nick, short_uid(uid)))
                else:
                    vals.append("%s（%s）· 旧格式需重建" % (nick, short_uid(uid)))
                continue
            marks = []
            if s.get("refreshExpiresAt") and s["refreshExpiresAt"] < now_ms:
                marks.append("令牌已失效需重登")
            if s.get("partial"):
                marks.append("仅凭证")
            # 只显示“月-日 时:分”，避免下拉框里时间被截断
            _t = (s["savedAt"] or "")[5:16] or "时间未知"
            vals.append("%s（%s）· %s" % (nick, short_uid(uid),
                                         "、".join(marks) if marks else _t))
        self.launch_cb.configure(values=vals)
        cur_info = read_auth_identity()
        cur_txt = ""
        if UID_RE.match(cur_info["uid"]):
            cur_txt = "　|　当前凭证：%s" % short_uid(cur_info["uid"])
        if vals:
            if self.launch_var.get() not in vals:
                self.launch_cb.current(0)
            n_ready = len(ready)
            n_old = sum(1 for _u, s in olds if s.get("format", 1) < 2)
            n_busy = len(olds) - n_old
            extra = ""
            if n_old:
                extra += "（其中 %d 个是旧格式，需重新保存）" % n_old
            if n_busy:
                extra += "（%d 个正在更新快照）" % n_busy
            self.snap_var.set("可切换账号 %d 个%s%s" % (n_ready, extra, cur_txt))
        else:
            self.launch_var.set("")
            self.snap_var.set("（暂无快照。在 WorkBuddy 里登录任意账号，工具会自动存档）")

    def refresh_env(self):
        running = app_running()
        cur = current_uid()
        if running:
            self.header.set_status("WorkBuddy 正在运行", "run")
            self.warn_var.set("⚠ WorkBuddy 还在运行，请先完全退出再迁移（否则修改可能被应用覆盖）。"
                              "可点右上角“退出 WorkBuddy”让它自动退（先温柔退出，无效会自动强制退出）；"
                              "关窗口只会缩到托盘，不算退出。")
        else:
            self.header.set_status("WorkBuddy 已退出", "ok")
            self.warn_var.set("")
        self.cur_uid = cur
        self.header.set_quit_enabled(running)

    def refresh_accounts(self):
        self.accounts = load_accounts()
        for i in self.acc_tree.get_children():
            self.acc_tree.delete(i)
        order = sorted(self.accounts.values(),
                       key=lambda a: (not a.get("is_current"), -(a.get("last") or 0)))
        # 本月各账号积分（用量统计还没跑完就先留空）
        month_first = datetime.date.today().replace(day=1).strftime("%Y-%m-%d")
        credit = {}
        for r in (self.usage or {}).get("by_account_day", []):
            if r["day"] >= month_first:
                credit[r["uid"]] = credit.get(r["uid"], 0.0) + r["credit"]
        for a in order:
            nick = a.get("nick") or "(未命名)"
            s = self.snapshots.get(a["uid"], {})
            if s.get("hasAuth"):
                snap = "已保存"
                if s.get("refreshExpiresAt") and s["refreshExpiresAt"] < int(time.time() * 1000):
                    snap = "令牌已失效"
                elif s.get("partial"):
                    snap = "仅凭证"
            elif s:
                snap = "旧格式需重建"
            else:
                snap = "—"
            cr = credit.get(a["uid"])
            self.acc_tree.insert(
                "", tk.END, iid=a["uid"],
                values=(nick + (" ★" if a.get("is_current") else ""),
                        a["uid"], a.get("sessions", 0),
                        (wb_usage.fmt_credit(cr) if (cr is not None and wb_usage) else "—"),
                        fmt_ts(a.get("last")), snap,
                        "是" if a.get("is_current") else ""))
        # 目标账号下拉
        vals = ["%s（%s）" % (a.get("nick") or "未命名", short_uid(a["uid"])) for a in order]
        self.target_cb.configure(values=vals)
        if vals:
            pick = 0
            for i, a in enumerate(order):
                if not a.get("is_current"):
                    pick = i
                    break
            self.target_cb.current(pick)
        # 只看账号下拉
        fvals = ["全部"] + ["%s（%s）" % (a.get("nick") or "未命名", short_uid(a["uid"]))
                            for a in order]
        self.filter_cb.configure(values=fvals)
        if self.filter_var.get() not in fvals:
            self.filter_var.set("全部")
        self._zebra(self.acc_tree)

    def refresh_sessions(self):
        self.sessions = load_sessions()
        for i in self.sess_tree.get_children():
            self.sess_tree.delete(i)
        self.tree_items = {}
        fsel = self.filter_var.get()
        kw = (self.search_var.get() or "").strip().lower()
        for d in self.sessions:
            a = self.accounts.get(d["user_id"], {})
            owner = "%s（%s）" % (a.get("nick") or "未命名", short_uid(d["user_id"]))
            if fsel != "全部" and owner != fsel:
                continue
            if kw and kw not in (d.get("title") or "").lower():
                continue
            mark = "☑" if d["id"] in self.sel else "☐"
            tags = []
            if d.get("transport") == "cloud":
                tags.append("cloud")
            if d.get("deleted_at"):
                tags.append("deleted")
            if not d.get("has_file"):
                tags.append("nofile")
            status = "已删除" if d.get("deleted_at") else (d.get("status") or "")
            iid = self.sess_tree.insert(
                "", tk.END, text=mark,
                values=(d["title"], owner, d.get("cwd") or "",
                        fmt_ts(d.get("last_activity_at") or d.get("updated_at")),
                        fmt_size(d.get("size")), d.get("transport") or "", status),
                tags=tags)
            self.tree_items[d["id"]] = iid
        self._zebra(self.sess_tree)

    def refresh_automations(self):
        self.automations = load_automations()
        for i in self.auto_tree.get_children():
            self.auto_tree.delete(i)
        self.auto_items = {}
        for d in self.automations:
            if d.get("deleted_at"):
                continue
            a = self.accounts.get(d.get("owner_user_id") or "", {})
            owner = "%s（%s）" % (a.get("nick") or "未命名", short_uid(d.get("owner_user_id") or ""))
            sched = d.get("rrule") or d.get("scheduled_at") or d.get("schedule_type") or ""
            mark = "☑" if d["id"] in self.sel_auto else "☐"
            iid = self.auto_tree.insert(
                "", tk.END, text=mark,
                values=(d.get("name") or "", owner, d.get("status") or "",
                        str(sched)[:60], fmt_ts(d.get("next_run_at"))))
            self.auto_items[d["id"]] = iid
        self._zebra(self.auto_tree)

    # ---------------- 交互

    def on_sess_click(self, event):
        iid = self.sess_tree.identify_row(event.y)
        if not iid:
            return
        sid = None
        for k, v in self.tree_items.items():
            if v == iid:
                sid = k
                break
        if not sid:
            return
        d = next((x for x in self.sessions if x["id"] == sid), None)
        if d and d.get("transport") == "cloud":
            self._dark_warn("无法迁移",
                            ["这是云端会话（transport=cloud），内容在服务器上、跟服务器账号绑定，",
                             "不能在本地两个账号之间迁移。"])
            return
        if sid in self.sel:
            self.sel.discard(sid)
            self.sess_tree.item(iid, text="☐")
        else:
            self.sel.add(sid)
            self.sess_tree.item(iid, text="☑")

    def on_auto_click(self, event):
        iid = self.auto_tree.identify_row(event.y)
        if not iid:
            return
        aid = None
        for k, v in self.auto_items.items():
            if v == iid:
                aid = k
                break
        if not aid:
            return
        if aid in self.sel_auto:
            self.sel_auto.discard(aid)
            self.auto_tree.item(iid, text="☐")
        else:
            self.sel_auto.add(aid)
            self.auto_tree.item(iid, text="☑")

    def select_all(self, on):
        for d in self.sessions:
            if d.get("transport") == "cloud":
                continue
            # 只动当前可见的
            if d["id"] not in self.tree_items:
                continue
            if on:
                self.sel.add(d["id"])
            else:
                self.sel.discard(d["id"])
            self.sess_tree.item(self.tree_items[d["id"]], text="☑" if on else "☐")

    def on_nick_edit(self, event):
        iid = self.acc_tree.identify_row(event.y)
        if not iid or iid not in self.accounts:
            return
        uid = iid
        old = self.accounts[uid].get("nick") or ""
        nick = simpledialog.askstring("改昵称", "给 %s 起个名字（比如：学校账号）："
                                   % short_uid(uid), initialvalue=old, parent=self.root)
        if nick is None:
            return
        nick = nick.strip()[:20]
        self.cfg.setdefault("nicks", {})[uid] = nick
        save_config(self.cfg)
        self.refresh_accounts()
        self.refresh_sessions()

    def target_uid(self):
        val = self.target_var.get()
        m = re.search(r"（([0-9a-fA-F-]{8})）\s*$", val)
        if not m:
            return ""
        head = m.group(1).lower()
        for uid in self.accounts:
            if uid.lower().startswith(head):
                return uid
        return ""

    # ---------------- 深色确认框（替掉系统 messagebox，跟界面同一套 HUD 风格）

    @staticmethod
    def _wrap_lines(text, limit=64):
        """按显示宽度折行：中文算 2 个单位、ASCII 算 1，空行原样保留。"""
        out = []
        for raw in str(text).splitlines():
            if not raw.strip():
                out.append("")
                continue
            cur, wide = "", 0
            for ch in raw:
                w = 2 if ord(ch) > 0x2E80 else 1
                if wide + w > limit and cur:
                    out.append(cur)
                    cur, wide = "   " + ch, 3 + w
                else:
                    cur += ch
                    wide += w
            out.append(cur)
        return out or [""]

    def _dark_box(self, title, lines, buttons, acc=None, hint="", tag="CONFIRM",
                  anchor=None):
        """通用自绘弹窗（v3.11 从 _dark_ask 抽出来）。

        切角玻璃面板 + 顶部冷光条 + 围绕的高亮环 + 自绘按钮；窗口四角用
        -transparentcolor 抠成真透明（v3.09）。居中于主窗口并按屏幕夹紧。

        buttons: [(key, 文字, 是否强调), ...]，从右往左排（第一个是最右的主按钮）。
        anchor: 居中参考窗口（默认主窗口；从别的置顶小窗里弹时传它）。
        返回被点掉的 key；Esc 等同最后一个按钮，Enter 等同第一个按钮。
        """
        PAD = 14
        acc = acc or C_ACC
        acc_txt = _mix(acc, "#ffffff", 0.45)
        KEY = "#01030a"
        # 关窗过程中 / 主窗口没了就别再弹，免得抛 TclError
        try:
            if getattr(self, "_closing", False) or not self.root.winfo_exists():
                return None
        except Exception:
            return None

        body = []
        for ln in lines:
            body.extend(self._wrap_lines(ln))
        MAXL = 20
        if len(body) > MAXL:
            body = body[:MAXL - 1] + ["…（另有 %d 行，详见日志框）" % (len(body) - MAXL + 1)]
        if not body:
            body = [""]

        # 行多时收紧行距，别让弹窗长到占满屏幕
        line_h = 23 if len(body) <= 12 else 20
        W = 520 + PAD * 2
        H = 100 + line_h * len(body) + 58 + PAD * 2
        max_h = 1000
        try:
            max_h = int(self.root.winfo_screenheight() * 0.78)
        except Exception:
            pass
        while len(body) > 3 and H > max_h:      # 屏幕装不下就砍行，别顶出屏幕
            body = body[:-1]
            body[-1] = "…（内容过长，已截断）"
            H = 100 + line_h * len(body) + 58 + PAD * 2

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.withdraw()
        key_ok = True
        try:
            win.attributes("-transparentcolor", KEY)
        except Exception:
            key_ok = False
        if not key_ok:
            KEY = C_PAGE
        win.configure(bg=KEY)
        try:
            win.transient(self.root)
        except Exception:
            pass
        try:
            ref = anchor if anchor is not None else self.root
            rx, ry = ref.winfo_rootx(), ref.winfo_rooty()
            rw, rh = ref.winfo_width(), ref.winfo_height()
            x = rx + (rw - W) // 2
            y = ry + max(60, (rh - H) // 3)
        except Exception:
            x, y = 420, 300
        x = max(12, min(x, win.winfo_screenwidth() - W - 12))
        y = max(12, min(y, win.winfo_screenheight() - H - 80))
        win.geometry("%dx%d+%d+%d" % (W, H, x, y))

        cv = tk.Canvas(win, width=W, height=H, bg=KEY,
                       highlightthickness=0, bd=0)
        cv.pack(fill=tk.BOTH, expand=True)

        state = {"hover": ""}
        res = {"key": None}

        px0, py0, px1, py1 = PAD, PAD, W - PAD, H - PAD

        # 按钮：从右往左排，主按钮在最右
        bw, bh = 104, 32
        oy = py1 - 38
        boxes = []
        rx = px1 - 20
        for key, label, primary in buttons:
            boxes.append((key, (rx - bw, oy, rx, oy + bh), label, primary))
            rx -= (bw + 10)
        boxes.reverse()

        def paint():
            cv.delete("all")
            # ① 贴边发光（最外层比卡片亮，压在卡片上不显黑）
            for d in range(PAD, 0, -1):
                t = 0.22 + 0.33 * (1.0 - d / float(PAD)) ** 2.0
                col = _mix(C_PAGE, acc, t)
                _cut_rect(cv, px0 - d, py0 - d, px1 + d, py1 + d,
                          14 + d, fill=col, outline=col)
            # ② 高亮环：外弱内强两道
            _cut_rect(cv, px0 - 7, py0 - 7, px1 + 7, py1 + 7, 20,
                      fill="", outline=_mix(C_PAGE, acc, 0.85), width=1)
            _cut_rect(cv, px0 - 5, py0 - 5, px1 + 5, py1 + 5, 18,
                      fill="", outline=_mix(acc, "#ffffff", 0.30), width=2)
            # ③ 面板本体
            _cut_rect(cv, px0, py0, px1, py1, 14, fill=C_CARD,
                      outline=_mix(C_CARD, acc, 0.45))
            _hgrad(cv, px0 + 16, py0 + 3, px1 - 16, py0 + 6,
                   _mix(C_CARD, acc, 0.20), _mix(C_CARD, acc, 0.90), steps=48)
            ix, iy = px0 + 32, py0 + 39
            cv.create_polygon(ix, iy - 9, ix + 9, iy, ix, iy + 9, ix - 9, iy,
                              fill=_mix(C_CARD, acc, 0.22), outline=acc)
            cv.create_text(ix, iy - 1, text="!", fill=acc,
                           font=(FONT_UI, 10, "bold"))
            cv.create_text(px0 + 54, py0 + 33, text=title, anchor="w",
                           fill=C_TXT, font=(FONT_UI, 12, "bold"))
            cv.create_text(px0 + 55, py0 + 51, text=_sp(tag), anchor="w",
                           fill=C_FAINT, font=(FONT_MONO, 7))
            cv.create_line(px0 + 20, py0 + 69, px1 - 20, py0 + 69, fill=C_LINE)
            for i, ln in enumerate(body):
                cv.create_text(px0 + 20, py0 + 93 + i * line_h, text=ln,
                               anchor="w", fill=C_TXT_DIM, font=(FONT_UI, 9))
            if hint:
                cv.create_text(px0 + 20, oy + bh / 2, text=hint, anchor="w",
                               fill=C_FAINT, font=(FONT_UI, 8))
            for key, box, label, primary in boxes:
                x0, y0, x1, y1 = box
                on = (state["hover"] == key)
                if primary:
                    fill = _mix(C_CARD, acc, 0.30 if on else 0.16)
                    line, fg = acc, (C_TXT if on else acc_txt)
                else:
                    fill = C_CARD2 if on else C_CARD
                    line = C_ACC if on else C_LINE_HI
                    fg = C_TXT if on else C_TXT_DIM
                _cut_rect(cv, x0, y0, x1, y1, 7, fill=fill, outline=line)
                cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                               fill=fg, font=(FONT_UI, 9))

        def hit(e):
            for key, box, label, primary in boxes:
                x0, y0, x1, y1 = box
                if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                    return key
            return ""

        def on_move(e):
            k = hit(e)
            if k != state["hover"]:
                state["hover"] = k
                try:
                    cv.configure(cursor="hand2" if k else "")
                except Exception:
                    pass
                paint()

        def on_leave(e=None):
            if state["hover"]:
                state["hover"] = ""
                paint()

        def finish(key):
            res["key"] = key
            try:
                cv.delete("all")
            except Exception:
                pass
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        def on_click(e):
            k = hit(e)
            if k:
                finish(k)

        cv.bind("<Motion>", on_move)
        cv.bind("<Leave>", on_leave)
        cv.bind("<Button-1>", on_click)
        win.bind("<Escape>", lambda e: finish(buttons[-1][0]))
        win.bind("<Return>", lambda e: finish(buttons[0][0]))
        win.bind("<KP_Enter>", lambda e: finish(buttons[0][0]))

        paint()
        try:
            win.deiconify()          # 画好再显示，避免先露一个矩形空窗
        except Exception:
            pass
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.lift()
        try:
            win.grab_set()
        except Exception:
            pass
        try:
            win.focus_force()
        except Exception:
            pass
        self.root.wait_window(win)
        return res["key"]

    def _dark_ask(self, title, lines, ok_text="确定", cancel_text="取消",
                  warn=False, hint="", anchor=None):
        """自绘确认框，返回 True/False（Esc=取消、Enter=确认）。"""
        acc = C_WARN if warn else C_ACC
        key = self._dark_box(title, lines,
                             [("ok", ok_text, True), ("cancel", cancel_text, False)],
                             acc=acc, hint=hint, anchor=anchor)
        return key == "ok"

    def _dark_info(self, title, lines, ok_text="知道了", kind="info",
                   hint="Enter / Esc 关闭", anchor=None):
        """自绘提示框（只有一个「知道了」）。kind: info / warn / error 决定强调色。"""
        acc = {"warn": C_WARN, "error": C_BAD}.get(kind, C_ACC)
        tag = {"warn": "NOTICE", "error": "ERROR"}.get(kind, "NOTICE")
        self._dark_box(title, lines, [("ok", ok_text, True)],
                       acc=acc, hint=hint, tag=tag, anchor=anchor)

    def _dark_error(self, title, lines, ok_text="知道了", anchor=None):
        self._dark_info(title, lines, ok_text=ok_text, kind="error", anchor=anchor)

    def _dark_warn(self, title, lines, ok_text="知道了", anchor=None):
        self._dark_info(title, lines, ok_text=ok_text, kind="warn", anchor=anchor)

    def on_quit_app(self):
        if not self._dark_ask(
                "退出 WorkBuddy",
                ["将关闭 WorkBuddy，未保存的内容可能丢失。",
                 "先温柔退出（最多等 12 秒），退不掉会自动强制退出。"],
                ok_text="退出", cancel_text="取消", warn=True,
                hint="Enter 确认  ·  Esc 取消"):
            return

        def work():
            if not app_running():
                self.root.after(0, self.refresh_all)
                return
            self.log("正在温柔退出 WorkBuddy（最多等 12 秒）……")
            self.root.after(0, self._start_quit_countdown)
            ok = quit_and_wait(timeout=12)
            self.root.after(0, self._stop_quit_countdown)
            if ok:
                self.log("WorkBuddy 已退出")
                self.root.after(0, self.refresh_all)
                return
            self.log("温柔退出无效，自动强制退出")
            self.root.after(0, lambda: self.header.set_quit_note("强制退出…"))
            force_quit_wb(self.log)
            for _ in range(10):
                if not app_running():
                    break
                time.sleep(0.5)
            self.root.after(0, self._stop_quit_countdown)
            self.root.after(0, self.refresh_all)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 退出按钮上的 12 秒倒计时

    def _start_quit_countdown(self, total=12):
        """点完「退出 WorkBuddy」，按钮立刻变成倒计时（按墙钟算，不受轮询节奏影响）。"""
        self._quit_cd_token = getattr(self, "_quit_cd_token", 0) + 1
        self._quit_cd_total = float(total)
        self._quit_cd_end = time.time() + float(total)
        self.header.set_quit_note("")
        self._tick_quit_cd(self._quit_cd_token)

    def _tick_quit_cd(self, token):
        if token != getattr(self, "_quit_cd_token", 0) or self._closing:
            return
        left = self._quit_cd_end - time.time()
        try:
            if left <= 0.02:
                self.header.set_quit_countdown(0)
                return
            self.header.set_quit_countdown(left, self._quit_cd_total)
            self.root.after(200, lambda t=token: self._tick_quit_cd(t))
        except Exception:
            pass

    def _stop_quit_countdown(self):
        """WorkBuddy 已退出 / 强制退出做完 → 结束倒计时并把按钮恢复原样。"""
        self._quit_cd_token = getattr(self, "_quit_cd_token", 0) + 1
        try:
            self.header.set_quit_countdown(0)
            self.header.set_quit_note("")
        except Exception:
            pass

    # ---------------- 执行迁移

    def on_go(self):
        if app_running():
            self._dark_error("先退出 WorkBuddy",
                             ["WorkBuddy 还在运行。请先完全退出，再运行迁移，",
                              "否则应用可能把修改覆盖掉。"])
            return
        target = self.target_uid()
        if not target:
            self._dark_error("没选目标账号", ["请先选择目标账号。"])
            return
        ids = [s for s in self.sel if s in self.tree_items]
        if not ids:
            self._dark_info("没选会话", ["请先勾选要迁移/复制的会话。"])
            return
        cur = getattr(self, "cur_uid", "")
        if cur and target == cur and self.mode.get() == "move":
            if not self._dark_ask(
                    "目标是当前登录账号",
                    ["目标账号就是当前登录的账号，迁移不会产生变化。",
                     "仍然继续吗？"]):
                return
        chosen = [d for d in self.sessions if d["id"] in ids]
        nofile = [d for d in chosen if not d.get("has_file")]
        mode = self.mode.get()
        mode_txt = "迁移（过继，原账号不再显示）" if mode == "move" else "复制（分身，两边都保留）"
        sample = "\n".join("  · " + (d.get("title") or "")[:60] for d in chosen[:12])
        more = "" if len(chosen) <= 12 else "\n  …等共 %d 个" % len(chosen)
        msg = ("模式：%s\n目标账号：%s\n\n将处理 %d 个会话：\n%s%s\n\n"
               "执行前会自动备份数据库。确定吗？" %
               (mode_txt, self.target_var.get(), len(chosen), sample, more))
        if nofile:
            msg += ("\n\n注意：有 %d 个会话在磁盘上找不到底稿（可能已被清理），"
                    "只改列表归属，内容可能不全。" % len(nofile))
        if mode == "copy":
            total = sum(d.get("size") or 0 for d in chosen)
            msg += ("\n\n复制模式会拷贝会话底稿（合计约 %s），大会话需要等一会。"
                    % fmt_size(total))
        if not self._dark_ask("确认执行", [msg], ok_text="开始执行"):
            return
        self.btn_go.configure(state=tk.DISABLED)
        threading.Thread(target=self._run, args=(ids, target, mode), daemon=True).start()

    def _run(self, ids, target, mode):
        try:
            bdir, n = backup_db("改动前")
            self.log("已备份数据库（%d 个文件）→ %s" % (n, bdir))
            if mode == "move":
                do_move(ids, target, self.log)
            else:
                do_copy(ids, target, self.log)
            self.log("完成。请重启 WorkBuddy，切换到目标账号即可看到这些会话。")
            self.root.after(0, lambda: self._dark_info(
                "完成",
                ["已处理 %d 个会话。" % len(ids), "",
                 "接下来：",
                 "1) 打开 WorkBuddy",
                 "2) 切换/登录到目标账号",
                 "3) 在会话列表里就能看到，点开继续", "",
                 "（万一不对，数据库已自动备份，可从「数据库备份」目录还原）"]))
        except Exception as e:
            msg = str(e)
            self.log("失败：%s\n%s" % (msg, traceback.format_exc()))
            self.root.after(0, lambda m=msg: self._dark_error(
                "出错了", ["迁移失败：%s" % m, "",
                          "数据库未遭破坏（已先备份）。",
                          "详细原因见日志框和 迁移日志.txt"]))
        finally:
            self.root.after(0, lambda: self.btn_go.configure(state=tk.NORMAL))
            self.root.after(0, self.refresh_all)

    def on_go_auto(self):
        if app_running():
            self._dark_error("先退出 WorkBuddy",
                             ["WorkBuddy 还在运行。请先完全退出，再运行迁移。"])
            return
        target = self.target_uid()
        if not target:
            self._dark_error("没选目标账号", ["请先选择目标账号。"])
            return
        ids = [a for a in self.sel_auto if a in self.auto_items]
        if not ids:
            self._dark_info("没选定时任务", ["请先勾选定时任务。"])
            return
        names = [d.get("name") or "" for d in self.automations if d["id"] in ids]
        if not self._dark_ask(
                "确认过户",
                ["把以下 %d 个定时任务的归属改给 %s："
                 % (len(ids), self.target_var.get())]
                + ["  · " + n[:50] for n in names]
                + ["", "执行前会自动备份。确定吗？"],
                ok_text="过户"):
            return
        threading.Thread(target=self._run_auto, args=(ids, target), daemon=True).start()

    def _run_auto(self, ids, target):
        try:
            bdir, n = backup_db("定时任务改动前")
            self.log("已备份数据库 → %s" % bdir)
            do_move_automation(ids, target, self.log)
            self.root.after(0, lambda: self._dark_info(
                "完成", ["已过户 %d 个定时任务。重启 WorkBuddy 后生效。" % len(ids)]))
        except Exception as e:
            msg = str(e)
            self.log("定时任务过户失败：%s" % msg)
            self.root.after(0, lambda m=msg: self._dark_error("出错了", [m]))
        finally:
            self.root.after(0, self.refresh_all)

    # ---------------- 自动登录：保存/切换启动

    def launch_target_uid(self):
        val = self.launch_var.get()
        m = re.search(r"（([0-9a-fA-F-]{8})）", val)
        if not m:
            return ""
        head = m.group(1).lower()
        for uid in self.snapshots:
            if uid.lower().startswith(head):
                return uid
        return ""

    def on_save_snapshot(self):
        info = read_auth_identity()
        if not UID_RE.match(info["uid"]):
            self._dark_info(
                "先登录",
                ["读不到登录凭证，说明这台机器上现在没有可用登录态。", "",
                 "请先打开 WorkBuddy 登录想保存的账号，再点本按钮。", "",
                 "凭证文件：%s" % (info["path"] or "（未找到 auth 目录）"),
                 info["error"] or ""])
            return
        cur = info["uid"]
        nick = self.accounts.get(cur, {}).get("nick", "")
        if not nick:
            nick = (read_account_snapshot() or {}).get("primary", {}).get("nickname", "")
        token_txt = "已过期（启动时自动用 refreshToken 续期）" if info["expired"] else "有效"
        if not self._dark_ask(
                "保存登录态",
                ["将把【当前登录账号】的登录凭证保存成快照：", "",
                 "  账号：%s（%s）" % (nick or "未命名", short_uid(cur)),
                 "  令牌：%s" % token_txt,
                 "  凭证：%s" % os.path.basename(info["path"]), "",
                 "步骤：退出 WorkBuddy（先温柔退出，无效会自动强制退出）",
                 "　　　→ 拷贝凭证文件 → 完成。", "",
                 "要支持几个账号，就各登录一次、各存一次。", "",
                 "确定吗？"],
                ok_text="保存"):
            return
        self.btn_save_snap.configure(state=tk.DISABLED)
        threading.Thread(target=self._run_save_snapshot, args=(cur, nick), daemon=True).start()

    def _quit_wb_with_guide(self, desc, cont):
        """确保 WorkBuddy 已退出，然后执行 cont()。
        退出策略（全自动，无需手动点托盘）：
        1) 温柔退出等 15 秒；2) 无效则自动强制退出（taskkill /F）；
        3) 仍活着才弹置顶引导窗兜底。必须在工作线程调用；
        cont 的异常在这里统一捕获弹框。"""
        if not app_running():
            self._cont_safe(desc, cont)
            return
        if quit_and_wait(timeout=15):
            self._cont_safe(desc, cont)
            return
        self.log("温柔退出无效（WorkBuddy 关窗口会缩托盘），自动强制退出")
        if force_quit_wb(self.log):
            for _ in range(10):
                if not app_running():
                    time.sleep(1.5)  # 给 Cookie 等文件落盘时间
                    self._cont_safe(desc, cont)
                    return
                time.sleep(0.5)
        self.log("自动退出未生效，转手动引导")
        self.root.after(0, lambda: self._show_quit_guide(desc, cont, 0))

    def _cont_safe(self, desc, cont):
        try:
            cont()
        except Exception as e:
            msg = str(e)
            self.log("%s失败：%s" % (desc, msg))
            self.root.after(0, lambda m=msg: self._dark_error("出错了", [m]))

    def _show_quit_guide(self, desc, cont, tries):
        """退出引导窗：教用户右键托盘退出；提供重试和强制退出兜底。"""
        win = tk.Toplevel(self.root)
        win.title("请先退出 WorkBuddy")
        win.attributes("-topmost", True)
        win.geometry("520x300")

        def _close():
            try:
                win.destroy()
            except Exception:
                pass
            self.btn_save_snap.configure(state=tk.NORMAL)
            self.btn_launch.configure(state=tk.NORMAL)

        ttk.Label(win, text="WorkBuddy 没有自动退出", wraplength=480,
                  font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(14, 6))
        ttk.Label(win, wraplength=480, text=(
            "工具已尝试自动退出 WorkBuddy（温柔退出 + 强制退出）但都没成功。"
            "WorkBuddy 点关闭窗口后会缩到任务栏右下角的托盘里继续后台运行，"
            "请这样手动退出：\n\n"
            "1) 在任务栏右下角找到 WorkBuddy 小图标\n"
            "2) 右键它 → 退出 / Quit\n\n"
            "退出后点“重试”；也可以再点一次“强制退出”试试。")).pack(padx=16, fill=tk.X)

        def _retry():
            _close()

            def work():
                if not app_running() or quit_and_wait(timeout=25):
                    self._cont_safe(desc, cont)
                    return
                if tries < 4:
                    self.root.after(0, lambda: self._show_quit_guide(desc, cont, tries + 1))
                else:
                    self.log("%s中止：多次重试后 WorkBuddy 仍在运行" % desc)
                    self.root.after(0, lambda: self._dark_error(
                        "还是没退出",
                        ["重试多次 WorkBuddy 仍在运行。",
                         "请手动右键托盘图标退出后，再重新点本工具里的按钮。"]))

            threading.Thread(target=work, daemon=True).start()

        def _force():
            _close()

            def work():
                if force_quit_wb(self.log) or not app_running():
                    time.sleep(1.5)
                    self._cont_safe(desc, cont)
                else:
                    self.log("%s中止：强制退出也失败了" % desc)

            threading.Thread(target=work, daemon=True).start()

        bf = ttk.Frame(win)
        bf.pack(pady=16)
        ttk.Button(bf, text="重试（我已退出）", command=_retry).pack(side=tk.LEFT, padx=8)
        ttk.Button(bf, text="强制退出", command=_force).pack(side=tk.LEFT, padx=8)
        ttk.Button(bf, text="取消", command=_close).pack(side=tk.LEFT, padx=8)
        win.protocol("WM_DELETE_BUTTON", _close)
        win.transient(self.root)

    def _run_save_snapshot(self, cur, nick):
        try:
            self._quit_wb_with_guide("保存登录态", lambda: self._do_save_snapshot(cur, nick))
        finally:
            self._suppress_auto()
            self.root.after(0, lambda: self.btn_save_snap.configure(state=tk.NORMAL))
            self.root.after(0, self.refresh_all)

    def _do_save_snapshot(self, cur, nick):
        save_login_snapshot(cur, nick, self.log)
        info = read_auth_identity()
        real = info["uid"] if UID_RE.match(info["uid"] or "") else cur
        token_txt = "已过期（启动时自动用 refreshToken 续期）" if info["expired"] else "有效"
        show_nick = nick or short_uid(real)
        self.root.after(0, lambda n=show_nick, u=short_uid(real), t=token_txt: self._dark_info(
            "已保存",
            ["已保存【%s】的登录凭证。" % n, "",
             "  账号：%s" % u,
             "  令牌：%s" % t, "",
             "现在可以：",
             "1) 重新打开 WorkBuddy（还是这个账号）；或",
             "2) 切到另一个账号再点一次本按钮 —— 两个都存好就能一键互切。"]))

    def on_launch_as(self):
        target = self.launch_target_uid()
        if not target:
            self._dark_info("没选账号", ["请先选择要以哪个账号启动。"])
            return
        s = self.snapshots.get(target, {})
        nick = s.get("nick", "")
        # 旧格式快照先拦住，别让用户白等一次退出
        if not s.get("hasAuth"):
            self._dark_warn(
                "这个快照要重建",
                ["【%s】的快照是 v2.0 之前的旧格式，里面没有登录凭证，"
                 % (nick or short_uid(target)),
                 "切过去仍会登回原账号。", "",
                 "请重建一次：",
                 "1) 在 WorkBuddy 里登录该账号",
                 "2) 回本工具点「保存当前登录态」", "",
                 "每个要切换的账号都做一遍，以后就一直能用。"])
            return
        if not self._dark_ask(
                "切换并启动",
                ["将以【%s】的登录态启动 WorkBuddy：" % (nick or short_uid(target)), "",
                 "1) 退出当前 WorkBuddy（先温柔退出，无效会自动强制退出）",
                 "2) 回装该账号的登录凭证",
                 "　　（当前登录态会自动备份到 登录态快照\\_回装前-时间戳）",
                 "3) 启动 WorkBuddy 并校验登的是谁", "",
                 "确定吗？"],
                ok_text="切换", warn=True):
            return
        self.btn_launch.configure(state=tk.DISABLED)
        threading.Thread(target=self._run_launch_as, args=(target, nick), daemon=True).start()

    def _run_launch_as(self, target, nick):
        try:
            self._quit_wb_with_guide("切换启动", lambda: self._do_launch_as(target, nick))
        finally:
            self._suppress_auto()
            self.root.after(0, lambda: self.btn_launch.configure(state=tk.NORMAL))
            self.root.after(0, self.refresh_all)

    def _do_launch_as(self, target, nick):
        restore_login_snapshot(target, nick, self.log)
        launch_workbuddy(self.log)
        self.log("等待 WorkBuddy 启动并确认账号……（最多 60 秒）")
        ok, why = False, ""
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(2.0)
            fp = auth_fingerprint()
            # 失败信号一：客户端写了登出标记（凭证被判无效）
            if fp["loggedOut"]:
                why = "客户端写了登出标记，说明凭证被判定无效"
                break
            # 失败信号二：客户端把账号改成了别人
            if UID_RE.match(fp["uid"]) and fp["uid"] != target:
                why = "客户端把账号改成了 %s" % short_uid(fp["uid"])
                break
            # 成功信号：进程探针报告本次启动登的就是目标账号
            got = crashpad_uid()
            if got:
                ok = (got == target)
                if not ok:
                    why = "进程报告的账号是 %s" % short_uid(got)
                break
        if not ok and not why:
            # 进程探针一直没返回：只要凭证没被改坏，就按成功处理
            fp = auth_fingerprint()
            if fp["uid"] == target and not fp["loggedOut"]:
                ok = True
                self.log("进程探针超时未返回，凭证仍为目标账号 → 按成功处理")
            else:
                why = "超时未探到进程账号"
        if ok:
            self.log("已以 %s 登录启动" % (nick or short_uid(target)))
            self.root.after(0, lambda: self._dark_info(
                "切换成功",
                ["WorkBuddy 已启动，当前登录：%s" % (nick or short_uid(target))]))
        else:
            self.log("切换未成功：%s" % (why or "未知原因"))
            self.root.after(0, lambda: self._show_switch_guide(target, nick, why))

    def _show_switch_guide(self, target, nick, why=""):
        """自动切换没成功时的兜底：置顶引导窗，用户手动切完点按钮检测（方案③半自动）。"""
        win = tk.Toplevel(self.root)
        win.title("请手动切换账号")
        win.attributes("-topmost", True)
        win.geometry("500x330")

        def _close():
            try:
                win.destroy()
            except Exception:
                pass

        ttk.Label(win, text="没能自动登上：%s" % (nick or short_uid(target)),
                  font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(14, 6))
        ttk.Label(win, wraplength=450, text=(
            ("检测结果：%s\n\n" % why if why else "") +
            "常见原因：该账号的令牌已失效（太久没登录，refreshToken 也过期了）。\n\n"
            "请在 WorkBuddy 里手动操作：\n"
            "左下角头像 → 切换账号 / 退出登录 → 用目标账号登录。\n\n"
            "登好之后点下面的按钮，工具会检测并帮你更新该账号的快照。")
        ).pack(padx=16, fill=tk.X)

        def _check():
            got = runtime_uid()
            if got == target:
                if self._dark_ask(
                        "已确认登录",
                        ["检测到当前登录就是：%s" % (nick or short_uid(target)), "",
                         "是否现在退出 WorkBuddy 并更新该账号的快照？",
                         "（更新后下次又能一键切换）"],
                        ok_text="更新", cancel_text="暂不", anchor=win):
                    _close()
                    self.btn_launch.configure(state=tk.DISABLED)
                    threading.Thread(target=self._run_update_after_manual,
                                     args=(target, nick), daemon=True).start()
                else:
                    _close()
            else:
                self._dark_warn(
                    "还没对上",
                    ["当前检测到的账号还不是目标账号（探到：%s）。" % (got or "未探到"), "",
                     "请确认已在 WorkBuddy 里切换到目标账号，再点按钮。"],
                    anchor=win)

        ttk.Button(win, text="我已登录好，重新检测", command=_check).pack(pady=16)
        win.protocol("WM_DELETE_BUTTON", _close)
        win.transient(self.root)

    def _run_update_after_manual(self, target, nick):
        try:
            self._quit_wb_with_guide("更新快照", lambda: self._do_update_after_manual(target, nick))
        finally:
            self._suppress_auto()
            self.root.after(0, lambda: self.btn_launch.configure(state=tk.NORMAL))
            self.root.after(0, self.refresh_all)

    def _do_update_after_manual(self, target, nick):
        save_login_snapshot(target, nick, self.log)
        self.root.after(0, lambda: self._dark_info(
            "快照已更新",
            ["已更新 %s 的登录态快照。" % (nick or short_uid(target)),
             "下次可直接一键切换。"]))

    # ---------------- 说明 / 目录

    def open_tool_dir(self):
        try:
            os.startfile(TOOL_DIR)
        except Exception:
            pass

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("使用说明 %s" % APP_VER)
        win.geometry("820x680")
        txt = ScrolledText(win, wrap=tk.WORD, font=(FONT_UI, 10),
                           bg="#ffffff", fg=C_TXT, relief="flat", padx=10, pady=10,
                           highlightthickness=0, bd=0)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        # 优先读同目录的《使用说明.txt》——改了 txt 不用重新打包 exe 也能看到新版
        text = HELP_TEXT
        if os.path.exists(README_PATH):
            try:
                for enc in ("gbk", "utf-8-sig", "utf-8"):
                    try:
                        text = open(README_PATH, "r", encoding=enc).read()
                        break
                    except UnicodeDecodeError:
                        continue
            except Exception:
                pass
        txt.insert(tk.END, text)
        txt.configure(state=tk.DISABLED)
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 10))


HELP_TEXT = """\
WorkBaby 账号及会话管理助手 · 使用说明（v3.12）
===============================================

工具位置：本文件（使用说明.txt / exe）所在的那个文件夹
打开方式（任选其一）：
  · 双击 "WorkBaby账号及会话管理助手_v<版本>.exe"   ← 推荐，独立程序，不用装 Python
  · 双击 "双击启动.bat"                             ← 用 Python 源码运行（改代码时用）

【免责声明 · 请先读这三行】
本工具是个人开发的第三方辅助程序，与 WorkBuddy 官方（腾讯）没有任何隶属、
合作或背书关系；"WorkBuddy" 等名称、商标归其权利人所有。
工具只读写你自己电脑上的本地文件，不联网、不上传任何数据；但账号切换与
会话迁移会改动工作目录里的凭证和数据库，请先自行备份、自担使用风险。
请仅用于管理你本人的账号，不得用于账号买卖、代登录等违规或商业用途。
作者不对使用本工具造成的任何直接或间接损失承担责任；代码以 MIT 协议开源。

一、它是干什么的
--------------
一台电脑登录过两个 WorkBuddy 账号时（比如“家用账号”和“学校账号”），
两边看到的历史会话（任务）互不相通：在 A 账号里做了一半的任务，
切换到 B 账号后找不到、也没法接着做。

本工具把“会话的归属账号”过户 / 复制给另一个账号。另一个账号登录后，
就能在列表里看到这些会话，点开接着聊。聊天内容原样保留。

除了迁移会话，工具现在还管这些事（界面左边四个页签）：

  · 账号     —— 账号一览：每个账号有多少会话、本月花了多少积分、
                登录态存了没有；“切换并启动”一键换号（见第七节）；
              “导出全部账号（加密）/ 从备份文件导入”（见第八节）。
  · 用量统计 —— 积分消耗、Token 用量、每日柱状图、账号排行、模型排行、
                模型限流与预计解封时间（见第八节）。
  · 会话迁移 —— 会话的迁移 / 复制，以及定时任务过户。
  · 日志     —— 本次与历史的操作记录。

一句话：会话能搬家，账号能一键换，用量能看清。

原理：会话列表在 C:\\Users\\Administrator\\.workbuddy\\workbuddy.db 的
sessions 表里，按 user_id 分账号；而聊天底稿（projects 目录里的 .jsonl）
是全盘共用的。所以只改 user_id，内容一个字节都不动。


二、上手三步
------------
1. 完全退出 WorkBuddy（工具顶部会检测，没退出不让动手）；
   也可以点工具右上角的“退出 WorkBuddy”，它会先温柔退出、
   不行再自动强制退出，不用去托盘点退出。
2. 左侧点“会话迁移”，勾选要交接的任务（可按账号筛选、可按标题搜索），
   选目标账号，选模式，点“开始执行”。执行前会自动备份数据库。
3. 重新打开 WorkBuddy，切换到目标账号（或用目标账号登录），
   会话列表里就能看到，点开继续。


三、两种模式的区别
------------------
· 迁移（过继）：任务搬到目标账号，原账号列表里不再显示。
  适合“这个任务交给另一个账号收尾”。
· 复制（分身）：目标账号多一个带“（互通副本）”标记的新会话，
  两边都能各自打开继续聊。注意：复制之后两边内容不再自动同步，
  只是开局相同，后面各聊各的。

想把 B 账号的会话给 A 账号：把“只看账号”选成 B、目标账号选 A 即可，
反之亦然。


四、怎么切换账号
----------------
两种方式，随你：

（1）手动：WorkBuddy 左下角头像 → “切换账号” / “退出登录”后换号登录。
（2）一键：工具「账号」页选账号 → 点「切换并启动」，详见第七节。

注意：两个账号都需要在这台电脑上至少登录过一次（凭据是本机生成的，
从没登录过的账号造不出来）。从没在本机登录过的，先在 WorkBuddy 里
登录一次，再回来用工具。


五、定时任务也能过户
--------------------
“定时任务 / 自动化”面板里勾选，点“迁移勾选的定时任务归属”按钮，
就把 owner 改给目标账号。不过户的话，切换账号后就看不到那条
监测 / 提醒了。


六、备份与日志
--------------
· 每次写库前自动备份到  数据库备份\\改动前-日期时间\\  目录，
  只保留最近 10 份。
· 要还原：完全退出 WorkBuddy，把备份里的 workbuddy.db、
  workbuddy.db-wal、workbuddy.db-shm 三个文件拷回
  C:\\Users\\Administrator\\.workbuddy\\ 覆盖即可。
· 登录态快照是“先写临时目录、整份成功后才替换”的，中途断电 / 保存失败
  不会把原快照弄坏（临时目录以 _tmp- 开头，扫描时会被跳过）。
· 所有操作记录在  迁移日志.txt。


七、自动登录：选谁，启动就是谁
------------------------------
WorkBuddy 没有“启动时指定账号”的功能，本工具用“登录凭证快照”实现：

1. 添加账号（工具开着就行）：在 WorkBuddy 里登录任意账号
   （左下角头像 → 切换账号 / 退出登录，再用目标账号登录）。
   工具会在后台自动把该账号的登录凭证存进  登录态快照\\<账号>\\ ，
   不用手动点保存。想顺手把 Cookie、浏览器状态也存全，可以在退出
   WorkBuddy 后补点一次「保存当前登录态」。
2. 以后切换：在“启动账号”下拉里选账号，点「切换并启动」。
   工具自动完成：退出当前 WorkBuddy（先温柔退出等 15 秒，无效就自动
   强制退出，不用手动点托盘）→ 回装该账号的登录凭证
   （当前登录态先备份到 登录态快照\\_回装前-时间戳\\）→ 启动 → 校验。
3. 某个账号很久没登录、令牌彻底失效时，启动后会弹引导窗，
   按提示在 WorkBuddy 里手动登录一次，工具会立刻把快照更新到最新。

快照里最关键的是“登录凭证”文件：
   %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
里面装的是该账号的 accessToken / refreshToken（它才决定“我是谁”）和
账号 uid。同一台电脑上加密钥匙是共用的，所以整份拷贝就能直接用，
不需要重新输密码、也不联网。

除凭证外，退出状态下保存还会带上 Cookie、请求缓存（local_storage）、
Chromium 的 Local Storage / Session Storage、账号指针
account-snapshot.json，让界面状态也跟着对上。每账号约 2-3MB。
工具在运行中只自动存凭证（Cookie 被应用锁着拷不全），这不影响登录，
下拉框里会标「仅凭证」。

注意：快照只对同一台电脑、同一个 Windows 用户有效；换电脑要重新保存。
工具不碰密码，只是在本机倒换登录凭证。
另：v2.0 之前保存的旧快照里没有登录凭证，切过去仍会登回原账号 ——
下拉框会标成“旧格式需重建”，在该账号登录一次工具就会自动补上。


八、用量统计与账号备份
----------------------
（1）用量统计：只看本机记录，不联网、不上传
    · 数据来自两处：workbuddy.db 的 session_usage 表（每次对话花了多少积分）
      + projects 目录里的聊天底稿（每条消息用的模型和 Token 数）。
    · 页面顶部可选「账号」和「范围（全部 / 近 7 天 / 近 30 天 / 本月）」，
      点「重新统计」重算。第一次统计要扫底稿，几秒钟，之后有缓存。
    · 四个数字卡片：积分消耗、Token 用量、对话回合、连续活跃天数。
    · 每日积分柱状图：最近每天花了多少积分。
    · 两个排行：账号用量排行（谁花得多）、模型用量排行（哪个模型烧积分）。
    · 模型限流：从底稿里的服务端错误提示（“将在 … 重置”）提取受限模型、
      最近一次受限时间、预计解封时间；已过时间的标“已解除”。
    · ★ 注意：这里的“积分”是本地记录的消耗量，不是官方剩余余额。
      想知道还剩多少积分，请到 WorkBuddy 客户端 / 官网查看。

（2）账号备份：加密导出、一键导入（换电脑用）
    · 「导出全部账号（加密）…」把 登录态快照 里所有账号的凭证打包成
      一个 .wbacc 文件，用你自己设的口令加密（PBKDF2 + HMAC-SHA256）。
    · 拷到另一台电脑，在那台机器的工具里点「从备份文件导入…」，
      输入同一个口令即可恢复所有账号，不用一个个重新登录。
    · 口令丢了打不开，谁也救不回来——请当密码一样记牢。
    · 这个文件等于你所有账号的登录权，别发微信、别放公开网盘。


九、常见问题
------------
Q：迁移后对方账号里看不到？
A：重启一次 WorkBuddy 再试；并确认切换/登录的是目标账号。

Q：云端会话（传输=cloud）为什么不能迁？
A：它的内容在服务器上，跟服务器账号绑定，本地改不了归属，工具会拦。

Q：复制大会话为什么慢？
A：要拷贝聊天底稿（可能上百 MB），属正常现象，等它跑完。

Q：能让两个账号实时看到同一份会话吗？
A：不能。“迁移”是唯一一份、换账号看；“复制”是两份独立的。
  应用本身不支持实时双向同步。

Q：项目记忆、工作区文件要迁吗？
A：不用。项目记忆在工作区里的 .workbuddy\\memory，跟账号无关，
  两个账号天然共享；聊天底稿也是全盘共用的，工具只改列表归属。

Q：自动登录用的“登录凭证快照”是什么？安全吗？
A：就是 WorkBuddy 自己存在本机的登录凭证文件。工具只是在账号之间
  倒换这些文件，不读密码、不联网；删掉“登录态快照”文件夹，
  重新登录一次就又有了。

Q：工具没开的时候登录了新账号，会不会漏掉？
A：不会。下次打开工具，它会比对当前登录凭证和已有快照，
  发现对不上就自动补存。

Q：为什么有的快照标「仅凭证」？
A：保存时 WorkBuddy 正在运行，Cookie 数据库被应用锁着拷不全，
  所以只存了决定登录身份的凭证文件。切换照样有效，不影响使用。

Q：切换后弹出“请手动切换账号”怎么办？
A：说明该账号的令牌连 refreshToken 都过期了（太久没登录）。
  在 WorkBuddy 里手动登录一次，点窗里的“我已登录好，重新检测”，
  工具会顺手把它的快照更新掉。之后又能一键切换。

Q：工具会把我的聊天记录传到别处吗？
A：不会。全部操作都在本机完成，不联网。

Q：能把“成长计划进度 / 每日签到 / 旅行状态”也显示出来吗？
A：不能。WorkBuddy 桌面客户端里没有这些功能（程序包里查不到相关界面
   和接口），也就没有可读的数据来源；“连续活跃天数”是按本机聊天记录
   算出来的真实数字。另外本工具对登录凭证只做整文件搬移、不解密，
   所以也调不了官方接口。

Q：能自动给多个账号签到吗？
A：做不到，原因同上（客户端没这个功能）。不建议强行逆向官方接口去
   模拟签到：接口一改就失效，多账号高频请求还有被风控的风险。

Q：积分不够时怎么知道该换哪个账号？
A：「账号」页会显示一句话建议：当前账号近 7 天用了多少、其他账号合计
   还有多少本地记录。想看真实余额请到客户端查看。


十、版本号与更新
----------------
版本号规则：小更新末位 +1（v3.02 → v3.03 → v3.04 …），
大改版才跳到新的大版本（v4.0）。

exe 文件名里带版本号，所以新旧版本可以并排放、不会互相覆盖；
确认新版好用后，把旧的那个 exe 删掉即可。程序顶部也显示当前版本。

改了源码之后重新出包：双击运行（或命令行执行）

    打包exe.py

它会自动：读源码里的版本号 → 打包 → 把
WorkBaby账号及会话管理助手_v<版本>.exe 输出到本文件夹。
出包前记得先把源码里的 APP_VER 改成新版本号。

数据文件（登录态快照\\、迁移日志.txt、tool-config.json、数据库备份\\）
都放在 exe 同一个文件夹里，换 exe 不影响这些数据。


十一、文件清单
--------------
WorkBaby账号及会话管理助手_v3.12.exe
                   独立程序（打包好了，双击即用，不用装 Python）
账号互通工具.py    主程序源码（Python + 图形界面，只用了系统自带组件）
打包exe.py         重新打包脚本（自动按版本号命名输出 exe）
app.ico            程序图标
wb_usage.py        用量统计模块（积分 / Token / 限流；缺了不影响其他功能）
wb_crypto.py       加密导出 / 导入模块（缺了不影响其他功能）
双击启动.bat       双击这个就能打开工具
使用说明.txt       就是本文件
tool-config.json   账号昵称记录（运行后自动生成）
迁移日志.txt       操作日志（运行后自动生成）
数据库备份\\        自动备份（运行后自动生成）
登录态快照\\        各账号登录凭证快照（运行后自动生成）
"""


# ======================================================================
# 视觉层：科技风自绘组件（v3.03 重做）
# ----------------------------------------------------------------------
# 为什么不用 ttk 样式：ttk 只能换颜色，做不出渐变、切角、发光、角标这些
# HUD 元素。这里全部用 Canvas 手绘：切角面板 + 渐变光条 + 细网格 + 呼吸灯。
# 组件自包含，不碰任何业务逻辑（两块锁定模块零改动）。
# ======================================================================

def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(c1, c2, t):
    """两色插值，t=0 → c1，t=1 → c2。"""
    r1, g1, b1 = _rgb(c1)
    r2, g2, b2 = _rgb(c2)
    return "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t + 0.5),
                              int(g1 + (g2 - g1) * t + 0.5),
                              int(b1 + (b2 - b1) * t + 0.5))


def _cut_rect(cv, x0, y0, x1, y1, cut=8, **kw):
    """切角矩形（HUD 面板基本形状）。比圆角更“工业”，且不用 arc 拼。"""
    pts = [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
           (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut)]
    flat = []
    for p in pts:
        flat.extend(p)
    return cv.create_polygon(flat, **kw)


def _vgrad(cv, x0, y0, x1, y1, c_top, c_bot, steps=24, tags=()):
    """竖直渐变（用一堆细线拼，Tk 没有原生渐变）。"""
    h = max(1, y1 - y0)
    step = h / float(steps)
    for i in range(steps):
        c = _mix(c_top, c_bot, i / float(max(1, steps - 1)))
        cv.create_line(x0, y0 + i * step, x1, y0 + i * step,
                       fill=c, width=int(step) + 1, tags=tags)


def _hgrad(cv, x0, y0, x1, y1, c_left, c_right, steps=40, tags=()):
    """水平渐变（卡片顶部那道冷光条）。"""
    w = max(1, x1 - x0)
    step = w / float(steps)
    for i in range(steps):
        c = _mix(c_left, c_right, i / float(max(1, steps - 1)))
        cv.create_line(x0 + i * step, y0, x0 + i * step, y1,
                       fill=c, width=int(step) + 1, tags=tags)


def _sp(text):
    """给英文小标题加字距（“MULTI-ACCOUNT” → “M U L T I ...”）。"""
    return " ".join(list(text))


class TechCard(tk.Frame):
    """数据卡：切角玻璃面 + 顶部冷光条 + 左上角标 + 大数字。

    对外仍然暴露 .var（StringVar），所以老的 `self.card_vars[k].set(...)`
    一行都不用改 —— 绑定 trace 自动重绘。
    """

    H = 88

    def __init__(self, parent, title, hint, accent=None):
        tk.Frame.__init__(self, parent, bg=C_PAGE, height=self.H)
        self.pack_propagate(False)
        self.accent = accent or C_ACC
        self.title = title
        self.hint = hint
        self.cv = tk.Canvas(self, bg=C_PAGE, height=self.H, highlightthickness=0, bd=0)
        self.cv.pack(fill=tk.BOTH, expand=True)
        self.var = tk.StringVar(value="…")
        self.var.trace_add("write", lambda *a: self._draw())
        self.cv.bind("<Configure>", lambda e: self._draw())

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w = cv.winfo_width() or 220
        h = self.H
        if w < 40:
            return
        x0, y0, x1, y1 = 1, 5, w - 2, h - 5
        # 面板 + 描边
        _cut_rect(cv, x0, y0, x1, y1, 9, fill=C_CARD, outline=C_LINE_HI)
        # 顶部冷光条：强调色 → 深色，一眼看出这是“活”的面板
        _hgrad(cv, x0 + 8, y0 + 1, x1 - 8, y0 + 3,
               _mix(self.accent, C_CARD, 0.18),
               _mix(self.accent, C_CARD, 0.88), steps=34)
        # 左上/右下角标（L 形，HUD 常见装饰）
        br = _mix(self.accent, C_CARD, 0.35)      # 角标压暗：装饰不该比文字亮
        cv.create_line(x0 + 10, y0 + 9, x0 + 10, y0 + 20, fill=br, width=1)
        cv.create_line(x0 + 10, y0 + 20, x0 + 21, y0 + 20, fill=br, width=1)
        cv.create_line(x1 - 10, y1 - 9, x1 - 10, y1 - 18, fill=_mix(self.accent, C_CARD, 0.55))
        cv.create_line(x1 - 10, y1 - 9, x1 - 19, y1 - 9, fill=_mix(self.accent, C_CARD, 0.55))
        # 标题
        cv.create_text(x0 + 25, y0 + 20, text=self.title, anchor="w",
                       fill=C_TXT_DIM, font=(FONT_UI, 9))
        # 提示（右上角小字）
        cv.create_text(x1 - 13, y0 + 20, text=self.hint, anchor="e",
                       fill=C_MUTED, font=(FONT_UI, 8))
        # 数值
        cv.create_text(x0 + 24, y0 + 51, text=self.var.get(), anchor="w",
                       fill=C_TXT, font=(FONT_MONO, 18, "bold"))
        # 底部装饰条（数据感的刻度）
        bx = x1 - 13
        for i, hh in enumerate((4, 7, 10, 6, 3)):
            cv.create_line(bx - i * 5, y1 - 10, bx - i * 5, y1 - 10 - hh,
                           fill=_mix(self.accent, C_CARD, 0.62), width=2)


class TechNav(tk.Frame):
    """左侧导航：深色 + 选中项发光指示条 + 图标。"""

    W = 138
    ROW_H = 48

    def __init__(self, parent, items, on_pick, on_help, on_dir):
        tk.Frame.__init__(self, parent, bg=C_NAV_BG, width=self.W)
        self.pack_propagate(False)
        self.cv = tk.Canvas(self, bg=C_NAV_BG, width=self.W, highlightthickness=0, bd=0)
        self.cv.pack(fill=tk.BOTH, expand=True)
        self.items = list(items)
        self.on_pick = on_pick
        self.on_help = on_help
        self.on_dir = on_dir
        self.cur = self.items[0][0] if self.items else ""
        self._hover = ""
        self.cv.bind("<Configure>", lambda e: self._draw())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(""))

    # ---- 内部
    def _hit(self, y):
        top = 26
        for i, it in enumerate(self.items):
            y0 = top + i * self.ROW_H
            if y0 <= y <= y0 + self.ROW_H:
                return it[0]
        return ""

    def _click(self, e):
        k = self._hit(e.y)
        if k:
            self.on_pick(k)
            return
        h = self.cv.winfo_height()
        if h - 88 <= e.y < h - 60:
            self.on_help()
        elif h - 60 <= e.y < h - 30:
            self.on_dir()

    def _motion(self, e):
        k = self._hit(e.y)
        if not k:
            h = self.cv.winfo_height()
            if h - 88 <= e.y < h - 60:
                k = "_help"
            elif h - 60 <= e.y < h - 30:
                k = "_dir"
        self._set_hover(k)

    def _set_hover(self, k):
        if k != self._hover:
            self._hover = k
            self._draw()

    # ---- 对外
    def select(self, key):
        self.cur = key
        self._draw()

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w = self.W
        h = cv.winfo_height() or 700
        # 底 + 右侧分隔
        _vgrad(cv, 0, 0, w, h, "#080e18", "#0a1420", steps=16)
        cv.create_line(w - 1, 0, w - 1, h, fill="#12212f")
        # 顶部标题（NAVIGATION）
        cv.create_text(18, 14, text=_sp("NAV"), anchor="w",
                       fill=C_FAINT, font=(FONT_MONO, 7))
        top = 26
        for i, (key, icon, label, _tip) in enumerate(self.items):
            y0 = top + i * self.ROW_H
            yc = y0 + self.ROW_H // 2
            on = (key == self.cur)
            hov = (key == self._hover and not on)
            if on:
                # 选中：切角底 + 左侧发光竖条 + 底部一道渐变线
                _cut_rect(cv, 8, y0 + 3, w - 10, y0 + self.ROW_H - 3, 7,
                          fill=C_NAV_ON, outline="#1c4f6e")
                cv.create_line(8, y0 + 8, 8, y0 + self.ROW_H - 8,
                               fill=C_ACC, width=2)
                _hgrad(cv, 10, y0 + self.ROW_H - 3, w - 12, y0 + self.ROW_H - 2,
                       C_ACC, _mix(C_ACC, C_NAV_BG, 0.9), steps=18)
            elif hov:
                _cut_rect(cv, 8, y0 + 3, w - 10, y0 + self.ROW_H - 3, 7,
                          fill="#0d1926", outline="#14283a")
            fg = C_TXT if on else (C_TXT_DIM if hov else C_MUTED)
            if on:
                fg = C_ACC
            cv.create_text(24, yc, text=icon, anchor="w",
                           fill=fg, font=(FONT_MONO, 11 if on else 10))
            cv.create_text(48, yc, text=label, anchor="w", fill=fg,
                           font=(FONT_UI, 10, "bold" if on else "normal"))
            if not on:
                cv.create_line(16, y0 + self.ROW_H - 1, w - 18, y0 + self.ROW_H - 1,
                               fill="#0e1a27")
        # 底部：使用说明 / 打开目录
        for i, (txt, hk) in enumerate((("使用说明", "_help"), ("打开工具目录", "_dir"))):
            y = h - 76 + i * 26
            on = (self._hover == hk)
            cv.create_text(20, y, text=txt, anchor="w",
                           fill=(C_ACC_TXT if on else C_MUTED),
                           font=(FONT_UI, 8))
        # 底部版本
        cv.create_text(20, h - 22, text="v" + APP_VER.lstrip("v") + "  ·  LOCAL",
                       anchor="w", fill="#3b5068", font=(FONT_MONO, 7))


class TechHeader(tk.Frame):
    """顶栏 HUD：渐变底 + 细网格 + 菱形 logo + 字距副标题 + 版本胶囊 +
    状态呼吸灯 + 功能按钮 + 最小化/最大化/关闭 + 底边流光。

    v3.04：系统标题栏整个去掉，所以最小化 / 最大化 / 关闭 三个按钮画在这里，
    行为跟系统一致（关闭 = 退本工具，不是退 WorkBuddy）。
    """

    H = 74

    def __init__(self, parent, title, ver, on_refresh, on_quit,
                 on_min=None, on_max=None, on_close=None):
        tk.Frame.__init__(self, parent, bg=C_HEAD_BG, height=self.H)
        self.pack_propagate(False)
        self.cv = tk.Canvas(self, bg=C_HEAD_BG, height=self.H, highlightthickness=0, bd=0)
        self.cv.pack(fill=tk.BOTH, expand=True)
        self.title = title
        self.ver = ver
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.on_min = on_min
        self.on_max = on_max
        self.on_close = on_close
        self.status = "检测中…"
        self.kind = "idle"          # idle / ok / run
        self.quit_enabled = False
        # v3.07：点「退出 WorkBuddy」后按钮变成倒计时（剩下的秒数 + 收缩的进度条）
        self.quit_cd = 0.0          # 倒计时剩余秒数，0 = 没在倒计时
        self.quit_cd_total = 12.0   # 总秒数（画进度条用）
        self.quit_note = ""         # 倒计时结束后接力的提示（如「强制退出…」）
        self.maxed = False
        self._phase = 0.0
        self._sx = 0
        self._hover = ""
        self.cv.bind("<Configure>", lambda e: self._draw())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(""))
        self.cv.bind("<Destroy>", self._on_destroy)
        self.after(150, self._tick)

    # ---- 对外：窗口控件状态
    def set_maxed(self, ok):
        self.maxed = bool(ok)
        self._draw()

    def zone_at(self, e):
        """返回鼠标所在按钮 key（没有则 None）。拖拽判定要用。"""
        for x0, y0, x1, y1, k in self._zones():
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                return k
        return None

    # ---- 对外
    def set_status(self, text, kind="idle"):
        self.status = text
        self.kind = kind
        self._draw()

    def set_quit_enabled(self, ok):
        self.quit_enabled = bool(ok)
        self._draw()

    def set_quit_countdown(self, left, total=None):
        """按钮上显示倒计时。left>0 → 显示「退出中 Ns」+ 收缩进度条；
        left<=0 → 恢复成「退出 WorkBuddy」。"""
        if total:
            self.quit_cd_total = float(total)
        self.quit_cd = max(0.0, float(left))
        self._draw()

    def set_quit_note(self, text):
        """倒计时结束后接力显示的一句话（如「强制退出…」）。"""
        self.quit_note = text or ""
        self._draw()

    @property
    def quit_busy(self):
        """正在退出（倒计时中或已进入强制退出阶段）→ 按钮不再响应点击。"""
        return bool(self.quit_cd > 0 or self.quit_note)

    # ---- 事件
    def _zones(self):
        """按钮热区（x0, y0, x1, y1, key）。右侧从外到内：
        关闭 / 最大化 / 最小化 / 退出 WorkBuddy / 重新检测。"""
        w = self.cv.winfo_width() or 1000
        bw, bh = 32, 24
        y0 = (self.H - bh) // 2
        y1 = y0 + bh
        right = w - 14
        z = [(right - bw, y0, right, y1, "close"),
             (right - 2 * bw - 4, y0, right - bw - 4, y1, "max"),
             (right - 3 * bw - 8, y0, right - 2 * bw - 8, y1, "min")]
        qx1 = right - 3 * bw - 22
        z.append((qx1 - 132, y0, qx1, y1, "quit"))
        rx1 = qx1 - 140
        z.append((rx1 - 100, y0, rx1, y1, "refresh"))
        return z

    def _click(self, e):
        for x0, y0, x1, y1, k in self._zones():
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                if k == "close":
                    if self.on_close:
                        self.on_close()
                elif k == "max":
                    if self.on_max:
                        self.on_max()
                elif k == "min":
                    if self.on_min:
                        self.on_min()
                elif k == "quit" and self.quit_enabled and not self.quit_busy:
                    self.on_quit()
                elif k == "refresh":
                    self.on_refresh()
                return

    def _motion(self, e):
        k = ""
        for x0, y0, x1, y1, kk in self._zones():
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                k = kk
        self._set_hover(k)

    def _set_hover(self, k):
        if k != self._hover:
            self._hover = k
            self.cv.configure(cursor="hand2" if k else "")
            self._draw()

    # ---- 动画：状态灯呼吸 + 底边流光
    def _on_destroy(self, e=None):
        """窗口销毁时掐掉动画，否则 after 回调会在控件没了之后触发（Tcl 报错）。"""
        if e is None or e.widget is self.cv:
            self._dead = True

    def _tick(self):
        if getattr(self, "_dead", False):
            return
        try:
            self._phase += 0.16
            import math
            # 呼吸灯外圈
            r = 5.0 + 2.0 * (0.5 + 0.5 * math.sin(self._phase))
            cx, cy = self._led_pos
            self.cv.coords(self._led_out, cx - r, cy - r, cx + r, cy + r)
            # 底边流光：整段右移，跑到头再从左绕回
            w = self.cv.winfo_width() or 1000
            self._sx += 4
            if self._sx > w + 240:
                self.cv.move("sweep", -(w + 480), 0)
                self._sx = -240
            else:
                self.cv.move("sweep", 4, 0)
            self.after(45, self._tick)
        except Exception:
            pass

    # ---- 绘制
    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w = cv.winfo_width() or 1000
        h = self.H
        # 底：竖直渐变（深空 → 夜蓝）
        _vgrad(cv, 0, 0, w, h, "#05080f", "#0b1a2b", steps=37)
        # 极淡竖向网格（20px 间距，right 侧更密的感觉靠网格本身）
        for x in range(0, w, 26):
            cv.create_line(x, 0, x, h, fill="#0a1520")
        # 顶/底边线：底边做三层渐变光，面板像“插着电”
        cv.create_line(0, 0, w, 0, fill="#0e1d2c")
        cv.create_line(0, h - 1, w, h - 1, fill="#1e5e75")
        cv.create_line(0, h - 2, w, h - 2, fill="#0d3a4a")
        cv.create_line(0, h - 3, w, h - 3, fill="#0c2b39")
        # 左侧斜向光带（装饰）
        for i in range(3):
            cv.create_line(6 + i * 3, h, 34 + i * 3, 0,
                           fill=_mix("#0b1a2b", C_ACC, 0.05 + i * 0.02))
        # ---- logo：切角方框 + W 折线
        _cut_rect(cv, 20, 21, 52, 53, 7, fill="#0a1a26", outline="#1e4a60")
        cv.create_line(26, 31, 30, 45, 34, 36, 38, 45, 42, 31,
                       fill=C_ACC, width=2, capstyle="round", joinstyle="round")
        cv.create_oval(44, 24, 48, 28, fill=C_ACC, outline="")
        # ---- 标题 + 副标题
        t = cv.create_text(64, 28, text=self.title, anchor="w",
                           fill=C_HEAD_TXT, font=(FONT_UI, 15, "bold"))
        bb = cv.bbox(t)
        vx = (bb[2] if bb else 300) + 12
        _cut_rect(cv, vx, 17, vx + 50, 39, 5, fill="#0b2a3a", outline="#1b4f66")
        cv.create_text(vx + 25, 28, text=self.ver, fill=C_ACC_TXT,
                       font=(FONT_MONO, 9, "bold"))
        cv.create_text(65, 50, text=_sp("MULTI-ACCOUNT  CONSOLE"), anchor="w",
                       fill=C_FAINT, font=(FONT_MONO, 7))
        # ---- 右侧按钮（切角）：功能按钮 + 窗口控件（— □ ✕）
        zones = self._zones()
        # 功能按钮与窗口控件之间的分隔线
        if len(zones) >= 5:
            dv = zones[3][0] - 8
            cv.create_line(dv, zones[3][1] + 2, dv, zones[3][3] - 2, fill="#1b3448")
        for x0, y0, x1, y1, k in zones:
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            on = (self._hover == k)
            if k in ("min", "max", "close"):
                if k == "close":                       # 关闭：hover 变红，最直观
                    fill = "#6e2530" if on else "#0b1620"
                    line = C_BAD if on else "#1c2c3c"
                    fg = "#ffffff" if on else C_TXT_DIM
                else:
                    fill = "#14293a" if on else "#0b1620"
                    line = C_LINE_HI if on else "#1c2c3c"
                    fg = "#dff6ff" if on else C_TXT_DIM
                _cut_rect(cv, x0, y0, x1, y1, 6, fill=fill, outline=line)
                if k == "min":
                    cv.create_line(cx - 5, cy + 3, cx + 5, cy + 3, fill=fg, width=2)
                elif k == "max":
                    if self.maxed:                     # 已最大化：画“还原”双框
                        cv.create_rectangle(cx - 6, cy - 6, cx + 3, cy + 3,
                                            outline=fg, width=1)
                        cv.create_rectangle(cx - 3, cy - 3, cx + 6, cy + 6,
                                            outline=fg, width=1)
                        cv.create_line(cx - 3, cy - 3, cx - 3, cy - 6, fill=fill)
                        cv.create_line(cx - 3, cy - 6, cx + 3, cy - 6, fill=fill)
                    else:
                        cv.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5,
                                            outline=fg, width=1)
                else:
                    cv.create_line(cx - 5, cy - 5, cx + 5, cy + 5, fill=fg, width=1.6)
                    cv.create_line(cx + 5, cy - 5, cx - 5, cy + 5, fill=fg, width=1.6)
                continue
            if k == "quit":
                en = self.quit_enabled
                if self.quit_cd > 0:
                    # v3.07：倒计时态（青色描边，字用强调色）
                    label = "退出中 %ds" % int(math.ceil(self.quit_cd))
                    fill, line, fg = "#0b1620", C_ACC, C_ACC_TXT
                elif self.quit_note:
                    # 温柔退出没成，进入强制退出阶段
                    label = self.quit_note
                    fill, line, fg = "#0b1620", C_WARN, C_WARN
                else:
                    label = "退出 WorkBuddy"
                    on = (on and en)
                    fill = "#14293a" if on else "#0b1620"
                    line = C_ACC if on else ("#234358" if en else "#16283a")
                    fg = ("#dff6ff" if on else (C_TXT_DIM if en else "#4a5c70"))
            else:
                label = "重新检测"
                fill = "#14293a" if on else "#0b1620"
                line = C_ACC if on else "#234358"
                fg = "#dff6ff" if on else C_TXT_DIM
            _cut_rect(cv, x0, y0, x1, y1, 7, fill=fill, outline=line)
            # 倒计时进度条：剩余比例从左往右缩，走完刚好清空
            # v3.10：原来是「内缩 4px 的直角矩形」塞在切角按钮里 —— 上下矮一截、
            # 四角还是直角，跟外框怎么都对不齐。改成贴着描边内侧（内缩 1px），
            # 左端跟着按钮一起切角，右端竖直平齐（填充中段本来就没有端头）。
            if k == "quit" and self.quit_cd > 0 and self.quit_cd_total > 0:
                frac = max(0.0, min(1.0, self.quit_cd / self.quit_cd_total))
                # ★ 内缩 0.5 而不是 1：Tk 填充多边形是“像素中心在内才填”，
                #   边界正好落在整数上会让最后一行像素漏掉（下沿露一条底色）。
                bx0, bty, bby = x0 + 0.5, y0 + 0.5, y1 - 0.5
                bar_w = (x1 - x0 - 1) * frac
                if bar_w >= 1.5:
                    xr = bx0 + bar_w
                    cut = 6.5
                    col = _mix("#0b1620", C_ACC, 0.30)
                    if bar_w > cut + 1.5:
                        cv.create_polygon(
                            bx0 + cut, bty, xr, bty, xr, bby, bx0 + cut, bby,
                            bx0, bby - cut, bx0, bty + cut,
                            fill=col, outline="")
                    else:              # 只剩最后一两像素，退化成细竖条
                        cv.create_rectangle(bx0, bty, xr, bby,
                                            fill=col, outline="")
            cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                           fill=fg, font=(FONT_UI, 9))
        # ---- 状态灯 + 文字
        color = {"idle": C_MUTED, "ok": C_OK, "run": C_BAD}.get(self.kind, C_MUTED)
        tx = zones[-1][0] - 14
        st = cv.create_text(tx, 36, text=self.status, anchor="e",
                            fill=color, font=(FONT_UI, 9))
        sb = cv.bbox(st)
        cx = (sb[0] if sb else tx - 40) - 13
        cy = 36
        self._led_pos = (cx, cy)
        self._led_out = cv.create_oval(cx - 6, cy - 6, cx + 6, cy + 6,
                                       fill=_mix(C_HEAD_BG, color, 0.22), outline="")
        cv.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5, fill=color, outline="")
        # ---- 底边流光（一段渐亮的光带，随时间右移）
        seg = 26
        for i in range(9):
            t = abs(i - 4) / 4.0
            c = _mix("#0a1c28", "#5fd3ea", max(0.0, 1 - t))
            cv.create_line(i * seg, h - 2, (i + 1) * seg, h - 2,
                           fill=c, width=2, tags=("sweep",))
        off = getattr(self, "_sx", 0)
        if abs(off) > 4:
            cv.move("sweep", off, 0)


def apply_theme(root):
    """深色科技风 ttk 皮肤（v3.03 重做）。

    clam 打底（唯一能自由改颜色的内置主题），控件全部改成深空底 + 冷蓝描边，
    主按钮用青色实心（深底上最跳），次要按钮压暗，避免和输入框糊在一起。
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    f = FONT_UI

    # 下拉列表（Tk 原生 Listbox，只能靠 option 数据库染色）
    try:
        root.option_add("*TCombobox*Listbox.background", "#0b1626")
        root.option_add("*TCombobox*Listbox.foreground", C_TXT)
        root.option_add("*TCombobox*Listbox.selectBackground", C_SEL)
        root.option_add("*TCombobox*Listbox.selectForeground", C_ACC)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
    except Exception:
        pass

    style.configure(".", font=(f, 9), background=C_PAGE, foreground=C_TXT,
                    bordercolor=C_LINE, focuscolor=C_ACC, troughcolor="#0a1420")
    style.configure("TFrame", background=C_PAGE)
    style.configure("Page.TFrame", background=C_PAGE)
    style.configure("Card.TFrame", background=C_CARD)
    style.configure("TLabel", background=C_PAGE, foreground=C_TXT)
    style.configure("Muted.TLabel", background=C_PAGE, foreground=C_MUTED, font=(f, 8))
    style.configure("Hint.TLabel", background=C_PAGE, foreground=C_MUTED, font=(f, 8))
    style.configure("Title.TLabel", background=C_PAGE, foreground=C_TXT, font=(f, 11, "bold"))

    # 按钮：深底 + 冷蓝描边，悬停提亮
    style.configure("TButton", background="#101d2c", foreground=C_TXT_DIM,
                    bordercolor=C_LINE_HI, lightcolor="#101d2c", darkcolor="#101d2c",
                    relief="flat", padding=(12, 6), font=(f, 9))
    style.map("TButton",
              background=[("pressed", "#0b1520"), ("active", "#16293c"),
                          ("disabled", "#0c141d")],
              foreground=[("disabled", "#4a5c70")],
              bordercolor=[("active", C_ACC)])
    # 主操作：青色实心 + 深字（深底上唯一的“亮块”，一眼找到）
    style.configure("Accent.TButton", background=C_ACC, foreground="#03222c",
                    bordercolor=C_ACC, lightcolor=C_ACC, darkcolor=C_ACC,
                    padding=(16, 6), font=(f, 9, "bold"))
    style.map("Accent.TButton",
              background=[("pressed", "#0f9dbf"), ("active", "#7fe0f2"),
                          ("disabled", "#123a47")],
              foreground=[("disabled", "#3c6d7c")],
              bordercolor=[("active", "#7fe0f2")])
    # 次要按钮：比输入框深一档
    style.configure("Subtle.TButton", background="#0d1826", foreground=C_MUTED,
                    bordercolor=C_LINE, lightcolor="#0d1826", darkcolor="#0d1826",
                    relief="flat", padding=(11, 5), font=(f, 9))
    style.map("Subtle.TButton",
              background=[("pressed", "#0a131e"), ("active", "#132437"),
                          ("disabled", "#0b1420")],
              foreground=[("disabled", "#4a5c70")],
              bordercolor=[("active", C_LINE_HI)])

    # 分组框：深底 + 冷蓝细边 + 青色小标题
    style.configure("TLabelframe", background=C_PAGE, bordercolor=C_LINE,
                    lightcolor=C_LINE, darkcolor=C_LINE,
                    relief="solid", borderwidth=1, padding=7)
    style.configure("TLabelframe.Label", background=C_PAGE, foreground=C_ACC_TXT,
                    font=(f, 9, "bold"))

    # 表格
    style.configure("Treeview", background=C_CARD, fieldbackground=C_CARD,
                    foreground=C_TXT_DIM, rowheight=31, borderwidth=0,
                    bordercolor=C_LINE, lightcolor=C_CARD, darkcolor=C_CARD,
                    font=(f, 10))
    style.configure("Treeview.Heading", background=C_CARD2, foreground=C_MUTED,
                    relief="flat", font=(f, 9, "bold"), padding=(6, 7),
                    bordercolor=C_LINE, lightcolor=C_CARD2, darkcolor=C_CARD2)
    style.map("Treeview.Heading", background=[("active", "#16283c")])
    style.map("Treeview", background=[("selected", C_SEL)],
              foreground=[("selected", "#eaf6ff")])

    # 输入框 / 下拉
    style.configure("TEntry", padding=(7, 5), fieldbackground=C_INPUT,
                    foreground=C_TXT, bordercolor=C_LINE, insertcolor=C_ACC,
                    lightcolor=C_INPUT, darkcolor=C_INPUT)
    style.map("TEntry", bordercolor=[("focus", C_ACC)],
              fieldbackground=[("disabled", "#0a121b")],
              foreground=[("disabled", "#4a5c70")])
    style.configure("TCombobox", padding=(6, 5), fieldbackground=C_INPUT,
                    background=C_INPUT, foreground=C_TXT,
                    bordercolor=C_LINE, arrowcolor=C_ACC,
                    lightcolor=C_INPUT, darkcolor=C_INPUT)
    style.map("TCombobox",
              fieldbackground=[("readonly", C_INPUT), ("disabled", "#0a121b")],
              selectbackground=[("readonly", C_INPUT)],
              selectforeground=[("readonly", C_TXT)],
              foreground=[("disabled", "#4a5c70")],
              bordercolor=[("focus", C_ACC), ("hover", C_LINE_HI)],
              arrowcolor=[("active", "#7fe0f2")])

    style.configure("TRadiobutton", background=C_PAGE, foreground=C_TXT_DIM, font=(f, 9))
    style.map("TRadiobutton", background=[("active", C_PAGE)],
              indicatorcolor=[("selected", C_ACC), ("!selected", "#16283c")],
              foreground=[("active", "#eaf6ff")])
    style.configure("TCheckbutton", background=C_PAGE, foreground=C_TXT_DIM, font=(f, 9))
    style.map("TCheckbutton", background=[("active", C_PAGE)],
              indicatorcolor=[("selected", C_ACC), ("!selected", "#16283c")],
              foreground=[("active", "#eaf6ff")])

    style.configure("TScrollbar", background="#14263a", troughcolor="#080f18",
                    bordercolor="#080f18", lightcolor="#14263a", darkcolor="#14263a",
                    arrowcolor="#4c7e9c", relief="flat", width=9)
    style.map("TScrollbar", background=[("active", "#2b5570")],
              arrowcolor=[("active", C_ACC)])
    style.configure("Vertical.TScrollbar", width=9)
    style.configure("Horizontal.TScrollbar", width=9)

def app_icon_path():
    """窗口/任务栏图标。打包后从解包目录取，源码运行从脚本目录取。"""
    bases = []
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", ""):
        bases.append(sys._MEIPASS)
    bases.extend([TOOL_DIR, _HERE])
    for b in bases:
        p = os.path.join(b, "app.ico")
        if os.path.exists(p):
            return p
    return ""


def win_hwnd(root):
    """取 Tk 顶层窗口的 Win32 HWND（Tk 自己的 winfo_id 是子窗口，要取 Parent）。"""
    try:
        from ctypes import windll
        return windll.user32.GetParent(root.winfo_id()) or 0
    except Exception:
        return 0


def install_frameless(root):
    """去掉系统标题栏（最小化/最大化/关闭那一条），但保留任务栏按钮。

    做法：overrideredirect(True) 让 Tk 走“无边框”路线（几何=像素，最干净），
    再补扩展样式：
      WS_EX_APPWINDOW  否则顶级窗口不在任务栏/Alt+Tab 里，最小化就找不回来了
      清 WS_EX_TOOLWINDOW / owner  Tk 给 override 窗口默认带这两个，会把任务栏按钮藏掉
    ★ Tk 在窗口映射时会把扩展样式整个重写一遍（实测 APPWINDOW 被抹掉），
      所以要挂 <Map> 再刷，并且延迟补两次，确保外壳登记了任务栏按钮。
    """
    from ctypes import windll

    GWL_EXSTYLE, GWL_HWNDPARENT = -20, -8
    WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = 0x00040000, 0x00000080

    def _apply(*_a):
        try:
            hwnd = win_hwnd(root)
            if not hwnd:
                return
            ex = windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ex = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            windll.user32.SetWindowLongW(hwnd, GWL_HWNDPARENT, 0)
            # ★ 只设样式，外壳并不会补任务栏按钮（实测没有）：必须让它把窗口
            #   “重新收起来再亮出来”一次。两步在同一个回调里连着调、中间不刷新，
            #   所以看不到闪。只做一次，之后靠上面的样式保持。
            if not getattr(root, "_wb_in_taskbar", False):
                windll.user32.ShowWindow(hwnd, 0)      # SW_HIDE
                windll.user32.ShowWindow(hwnd, 5)      # SW_SHOW
                root._wb_in_taskbar = True
        except Exception:
            pass

    try:
        root.overrideredirect(True)
        root.bind("<Map>", _apply, add="+")
        _apply()
        for d in (1, 120, 400):
            try:
                root.after(d, _apply)
            except Exception:
                break
        return True
    except Exception:
        return False


def work_area(root=None):
    """Windows 工作区（屏幕去掉任务栏后的矩形）→ (x, y, w, h)。

    自己算，是为了让“最大化”不要盖住任务栏（无边框窗口系统不让它自己算）。
    """
    try:
        from ctypes import windll, byref, Structure, c_int

        class _RECT(Structure):
            _fields_ = [("left", c_int), ("top", c_int),
                        ("right", c_int), ("bottom", c_int)]

        rc = _RECT()
        if windll.user32.SystemParametersInfoW(0x0030, 0, byref(rc), 0):
            return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
    except Exception:
        pass
    if root is not None:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    return 0, 0, 0, 0


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()                 # 先藏起来，画完再露脸，避免白框闪一下
    install_frameless(root)         # 去系统标题栏（任务栏按钮保留）
    root.configure(bg=C_PAGE)
    apply_theme(root)
    App(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
