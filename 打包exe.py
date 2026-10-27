# -*- coding: utf-8 -*-
"""打包exe.py —— 一键把工具打成独立 exe（自动按源码版本号命名）

用法：
    双击 打包exe.bat        （推荐）
    或   python 打包exe.py
    或   python 打包exe.py --console   （出带黑窗口的调试版）

它做四件事：
  1. 从 账号互通工具.py 里读 APP_VER（如 v3.12）→ 输出名 WorkBaby账号及会话管理助手_v3.12.exe
  2. 调 PyInstaller 打包（单文件、无控制台、带图标、内嵌 wb_usage/wb_crypto）
  3. 把 exe 复制到工具根目录（和 登录态快照/迁移日志 放一起，数据直接复用）
  4. 打印结果，旧版本的 exe 不动（方便回退）

注意：出包前先把源码里的 APP_VER 改成新版本号。
"""
import os
import re
import shutil
import subprocess
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(TOOL_DIR, "账号互通工具.py")
ICON = os.path.join(TOOL_DIR, "app.ico")
DIST = os.path.join(TOOL_DIR, "dist")
BUILD = os.path.join(TOOL_DIR, ".workbuddy", "tmp", "build_exe")

# 这些包本项目用不到，排掉能明显减小体积、避免误拉进来
EXCLUDES = ("PIL", "numpy", "pandas", "matplotlib", "customtkinter",
            "scipy", "IPython", "notebook", "pytest")


def read_version():
    src = open(MAIN_PY, encoding="utf-8").read()
    m = re.search(r'^APP_VER\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit("读不到 账号互通工具.py 里的 APP_VER，请检查源码")
    return m.group(1)


def main():
    console = "--console" in sys.argv
    if not os.path.exists(MAIN_PY):
        raise SystemExit("找不到主程序：%s" % MAIN_PY)

    ver = read_version()
    suffix = "_debug" if console else ""
    name = "WorkBaby账号及会话管理助手_%s%s" % (ver, suffix)
    target = os.path.join(TOOL_DIR, name + ".exe")

    print("=" * 60)
    print("  打包 WorkBaby 账号及会话管理助手 %s" % ver)
    print("  输出：%s" % name + ".exe")
    print("  方式：%s" % ("带控制台（调试用）" if console else "无控制台（正式版）"))
    print("=" * 60)

    if os.path.exists(target):
        try:  # 旧文件被占用（正在运行）就提示，别继续白跑
            os.rename(target, target + ".tmp")
            os.rename(target + ".tmp", target)
        except OSError:
            raise SystemExit("\n[中止] %s 正在运行，请先关掉它再打包。\n" % (name + ".exe"))

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--onefile",
           "--console" if console else "--windowed",
           "--name", name,
           "--icon", ICON,
           "--add-data", "%s%s." % (ICON, os.pathsep),
           "--hidden-import", "wb_usage",
           "--hidden-import", "wb_crypto",
           "--distpath", DIST,
           "--workpath", BUILD,
           "--specpath", BUILD,
           ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(MAIN_PY)

    t0 = __import__("time").time()
    r = subprocess.run(cmd, cwd=TOOL_DIR)
    if r.returncode != 0:
        raise SystemExit("\n[失败] PyInstaller 返回 %d，往上翻看报错。\n" % r.returncode)

    built = os.path.join(DIST, name + ".exe")
    if not os.path.exists(built):
        raise SystemExit("\n[失败] 没找到产物：%s\n" % built)

    shutil.copy2(built, target)
    size = os.path.getsize(target) / (1024.0 * 1024.0)
    print("\n" + "=" * 60)
    print("  出包成功！%.1f 秒，%.1f MB" % (__import__("time").time() - t0, size))
    print("  %s" % target)
    print("  双击它就能用；数据（登录态快照/迁移日志）都在同一目录。")
    print("=" * 60)


if __name__ == "__main__":
    main()
