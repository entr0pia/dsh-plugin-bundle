#!/usr/bin/env python3
"""Zero-window fast-checker and launcher for agy CLI with auto-patching (context + region)."""

import json
import os
import subprocess
import sys


def get_paths():
    local_app = os.environ.get("LOCALAPPDATA", "")
    user_dir = os.environ.get("USERPROFILE", "")
    tools_dir = os.path.join(user_dir, r".gemini\antigravity\tools")
    
    agy_exe = os.path.join(local_app, r"agy\bin\agy.exe")
    ls_exe = os.path.join(
        local_app, r"Programs\antigravity\resources\bin\language_server.exe"
    )
    patcher = os.path.join(tools_dir, "agy-patch.py")
    region_patcher = os.path.join(tools_dir, "agy-region-patch.py")
    stamp_file = os.path.join(tools_dir, "patch-stamp.json")
    
    return agy_exe, ls_exe, patcher, region_patcher, stamp_file


def load_stamp(stamp_path):
    if not os.path.exists(stamp_path):
        return {}
    try:
        with open(stamp_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_stamp(stamp_path, data):
    try:
        with open(stamp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def get_file_info(path):
    if not os.path.exists(path):
        return None
    try:
        st = os.stat(path)
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except Exception:
        return None


def patch_binary(patcher, binary_path, limit="512k"):
    if not os.path.exists(patcher) or not os.path.exists(binary_path):
        return False
    try:
        res = subprocess.run(
            [
                sys.executable,
                patcher,
                "--binary",
                binary_path,
                "--in-place",
                "--no-backup",
                "--limit",
                limit,
            ],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            timeout=20,
        )
        return res.returncode == 0
    except Exception:
        return False


def patch_region(region_patcher, binary_path):
    if not os.path.exists(region_patcher) or not os.path.exists(binary_path):
        return False
    try:
        res = subprocess.run(
            [
                sys.executable,
                region_patcher,
                "--apply",
                binary_path,
            ],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            timeout=20,
        )
        return res.returncode == 0
    except Exception:
        return False


def check_and_patch(agy_exe, ls_exe, patcher, region_patcher, stamp_file):
    stamp = load_stamp(stamp_file)
    changed = False

    # 1. 检查 CLI agy.exe
    agy_info = get_file_info(agy_exe)
    if agy_info:
        cached = stamp.get("agy_exe")
        if not cached or cached.get("size") != agy_info["size"] or cached.get("mtime_ns") != agy_info["mtime_ns"]:
            # 1.1 512K 上下文窗口解锁
            patch_binary(patcher, agy_exe, limit="512k")
            # 1.2 地区限制解锁
            patch_region(region_patcher, agy_exe)
            new_info = get_file_info(agy_exe)
            if new_info:
                stamp["agy_exe"] = new_info
                changed = True

    # 2. 检查桌面端 language_server.exe
    ls_info = get_file_info(ls_exe)
    if ls_info:
        cached = stamp.get("ls_exe")
        if not cached or cached.get("size") != ls_info["size"] or cached.get("mtime_ns") != ls_info["mtime_ns"]:
            patch_binary(patcher, ls_exe, limit="512k")
            new_info = get_file_info(ls_exe)
            if new_info:
                stamp["ls_exe"] = new_info
                changed = True

    if changed:
        save_stamp(stamp_file, stamp)


def main():
    agy_exe, ls_exe, patcher, region_patcher, stamp_file = get_paths()

    check_only = "--check-only" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--check-only"]

    # 运行轻量级检测与打补丁 (上下文 + 地区)
    check_and_patch(agy_exe, ls_exe, patcher, region_patcher, stamp_file)

    if check_only:
        return

    # 若非 check-only 模式，拉起 agy.exe 并透传参数
    if os.path.exists(agy_exe):
        try:
            res = subprocess.run([agy_exe] + args)
            sys.exit(res.returncode)
        except Exception:
            pass


if __name__ == "__main__":
    main()
