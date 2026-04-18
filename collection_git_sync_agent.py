#!/usr/bin/env python3
"""
Collection list → GitHub 同步代理
================================
监听 `collection_list/` 目录下的文件变更（新增、修改、删除），在短暂静默后自动执行：
  git add → git commit → git push

用法：
  export GIT_REMOTE=origin          # 可选，默认 origin
  export GIT_BRANCH=main            # 可选，自动检测失败时用 main
  python collection_git_sync_agent.py

前置条件：
  1. 本目录已 `git init` 且 `git remote add origin <你的仓库 URL>`
  2. 已配置推送方式（SSH 密钥，或 HTTPS + credential helper），勿把密码写入仓库
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 默认监听目录与仓库根目录（均为本脚本所在目录）
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(os.environ.get("AWESOME_PAPERS_ROOT", str(SCRIPT_DIR)))
WATCH_DIR = REPO_ROOT / "collection_list"
DEBOUNCE_SEC = float(os.environ.get("SYNC_DEBOUNCE_SEC", "3"))
GIT_REMOTE = os.environ.get("GIT_REMOTE", "origin")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "")  # 空则自动检测当前分支

_timer: threading.Timer | None = None
_timer_lock = threading.Lock()


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _detect_branch() -> str:
    if GIT_BRANCH:
        return GIT_BRANCH
    p = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    return "main"


def _has_changes() -> bool:
    st = _run_git(["status", "--porcelain"], check=False)
    return bool(st.stdout.strip())


def _push_to_github() -> None:
    if not (REPO_ROOT / ".git").is_dir():
        print("[sync] 错误: 尚未初始化 Git。请在 awesome_papers 下执行: git init && git remote add origin <url>", file=sys.stderr)
        return

    if not WATCH_DIR.is_dir():
        print(f"[sync] 警告: 监听目录不存在，已跳过推送: {WATCH_DIR}", file=sys.stderr)
        return

    branch = _detect_branch()

    # 暂存整个仓库（与「把整个 awesome_papers 推到 GitHub」一致）
    r = _run_git(["add", "-A"], check=False)
    if r.returncode != 0:
        print(f"[sync] git add 失败:\n{r.stderr}", file=sys.stderr)
        return

    if not _has_changes():
        print("[sync] 无变更，跳过 commit/push")
        return

    msg = f"chore: sync collection_list ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    c = _run_git(["commit", "-m", msg], check=False)
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        print(f"[sync] git commit 失败:\n{c.stderr or c.stdout}", file=sys.stderr)
        return

    p = _run_git(["push", GIT_REMOTE, branch], check=False)
    if p.returncode != 0:
        print(f"[sync] git push 失败:\n{p.stderr or p.stdout}", file=sys.stderr)
        return

    print(f"[sync] 已推送 {GIT_REMOTE}/{branch}")


def _debounced_push() -> None:
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE_SEC, _push_to_github)
        _timer.daemon = True
        _timer.start()
        print(f"[sync] 检测到变更，{DEBOUNCE_SEC}s 后推送…")


def main() -> None:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("请先安装: pip install watchdog", file=sys.stderr)
        sys.exit(1)

    if not WATCH_DIR.is_dir():
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[sync] 已创建监听目录: {WATCH_DIR}")

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):  # type: ignore[override]
            if event.is_directory:
                return
            # 忽略临时/编辑器备份
            name = Path(event.src_path).name
            if name.startswith(".") or name.endswith("~"):
                return
            _debounced_push()

    print(f"[sync] 仓库根目录: {REPO_ROOT}")
    print(f"[sync] 监听目录: {WATCH_DIR}")
    print(f"[sync] 防抖: {DEBOUNCE_SEC}s | remote={GIT_REMOTE} branch={_detect_branch()!r}")
    print("[sync] 按 Ctrl+C 退出\n")

    obs = Observer()
    obs.schedule(_Handler(), str(WATCH_DIR), recursive=True)
    obs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join(timeout=5)


if __name__ == "__main__":
    main()
