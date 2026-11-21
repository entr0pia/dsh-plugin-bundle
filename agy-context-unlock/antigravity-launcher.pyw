#!/usr/bin/env python3
"""Zero-window launcher for Antigravity with auto-patching of language_server.exe (context + region)."""

import os
import subprocess
import sys


def main():
    local_app = os.environ.get("LOCALAPPDATA", "")
    user_dir = os.environ.get("USERPROFILE", "")

    ls_bin = os.path.join(
        local_app, r"Programs\antigravity\resources\bin\language_server.exe"
    )
    tools_dir = os.path.join(user_dir, r".gemini\antigravity\tools")
    patcher = os.path.join(tools_dir, "agy-patch.py")
    region_patcher = os.path.join(tools_dir, "agy-desktop-region-patch.py")
    app_exe = os.path.join(local_app, r"Programs\antigravity\Antigravity.exe")

    # 1. 尝试静默就地 Patch（已解锁时两脚本均在 0.1s 内判定并退出，不产生写操作）
    if os.path.exists(ls_bin):
        # 1.1 512K 上下文解锁
        if os.path.exists(patcher):
            try:
                subprocess.run(
                    [
                        sys.executable,
                        patcher,
                        "--binary",
                        ls_bin,
                        "--in-place",
                        "--no-backup",
                        "--limit",
                        "512k",
                    ],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                pass

        # 1.2 地区限制解锁
        if os.path.exists(region_patcher):
            try:
                subprocess.run(
                    [
                        sys.executable,
                        region_patcher,
                        "--apply",
                        ls_bin,
                    ],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                pass

    # 2. 启动 Antigravity 客户端，并转发所有命令行传入的参数（如打开的文件或工程目录）
    if os.path.exists(app_exe):
        args = [app_exe] + sys.argv[1:]
        subprocess.Popen(
            args,
            cwd=os.path.dirname(app_exe),
        )


if __name__ == "__main__":
    main()
