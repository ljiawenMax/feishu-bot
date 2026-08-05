"""快照 / 回滚：保护「主程序目录」这一个面。

范围只有 `/home/ljiawen/project/feishu-bot`（含 .git / .env.local / .claude/，排除 .venv 与
缓存）。其余 9 个 bot 的 work_dir **不进快照、不回滚**——它们跑的是同一份代码，主程序活着
就能通过飞书把它们修回来；而 work_dir 里全是用户数据（resume 档案、stock 数据、wiki 索引），
回滚等于删用户的东西。

基线语义（关键，别改坏）：known-good = 「**某一轮任务开始之前**的状态」，由 bot.py 在调
run_claude 前 take()、该轮 LLM 成功后 promote() 转正。不是「上次重启前」也不是「当前状态」：
写坏配置的那一轮，它自己往往是成功的（正是它写坏的），拿完成时刻的状态当基线会把污染态存成
known-good，回滚过去等于没回滚。任务前拍照让基线天然早于任何写操作，这个竞态从根上消掉。

去重靠 rsync --link-dest 硬链到上一份，除首份外每份只占改动量（整目录约 1.6M）。
"""

import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = os.environ.get("GUARD_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.environ.get("GUARD_STORE") or os.path.expanduser("~/feishu-bot-snapshots")

SNAP_DIR = os.path.join(STORE, "snap")
KNOWN_GOOD = os.path.join(STORE, "known-good")   # symlink → snap/<id>
LOCK_PATH = os.path.join(STORE, ".lock")

# 保留份数（known-good 指向的那份永不回收）
KEEP = 30

# 排除项。两个有讲究的：
#   .venv/  231M，装出来的东西，不是被 claude 改坏的面，回滚它既慢又没意义
#   .git/   **刻意排除**。回滚 .git 会连真实提交历史和 gitsync 的基线 ref
#           （refs/feishu-sync/<chat_id>）一起倒回去——那是用户数据，不在回滚范围（§4.6）；
#           而且 rsync 撞上并发的 git commit 有拍到半写状态的风险，排除掉这个风险就没了。
#           代价：回滚后工作树与 HEAD 不一致，下一轮 gitsync 会把这次回滚作为一个正常提交
#           记进历史——反而更诚实。注意这也意味着 .git/hooks 里的东西回滚不掉（gitsync 走
#           --no-verify，影响有限）。
EXCLUDES = [".venv/", ".git/", "__pycache__/", "*.pyc", ".mypy_cache/", ".pytest_cache/", ".rwtest"]


def _ensure_store():
    os.makedirs(SNAP_DIR, exist_ok=True)


class _Lock:
    """跨进程互斥：10 个 bot 的 worker 线程 + 看门狗可能同时动快照目录。"""

    def __enter__(self):
        _ensure_store()
        self.fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _rsync(src, dst, delete=False, link_dest=None):
    cmd = ["rsync", "-a"]
    if delete:
        cmd.append("--delete")
    for e in EXCLUDES:
        cmd += ["--exclude", e]
    if link_dest:
        cmd += ["--link-dest", link_dest]
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def list_snapshots():
    """按时间正序返回快照 id（id 本身是可排序的时间戳串）。"""
    if not os.path.isdir(SNAP_DIR):
        return []
    return sorted(d for d in os.listdir(SNAP_DIR) if os.path.isdir(os.path.join(SNAP_DIR, d)))


def known_good_id():
    """当前基线的快照 id；没有基线或 symlink 悬空时返回 None。"""
    try:
        target = os.readlink(KNOWN_GOOD)
    except OSError:
        return None
    sid = os.path.basename(target.rstrip("/"))
    return sid if os.path.isdir(os.path.join(SNAP_DIR, sid)) else None


def take():
    """拍一份快照，返回其 id。调用点：bot.py 在 run_claude **之前**。"""
    with _Lock():
        prev = list_snapshots()
        sid = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dst = os.path.join(SNAP_DIR, sid)
        _rsync(ROOT, dst, link_dest=os.path.join(SNAP_DIR, prev[-1]) if prev else None)
        _gc()
        return sid


def promote(sid):
    """把某份快照转正为 known-good。只往前推：并发任务乱序完成时保留更新的那份
    （它是在已成功的那轮运行期间拍的，同样被证明可用）。"""
    if not sid or not os.path.isdir(os.path.join(SNAP_DIR, sid)):
        return False
    with _Lock():
        cur = known_good_id()
        if cur and cur >= sid:
            return False
        tmp = KNOWN_GOOD + ".tmp"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.join("snap", sid), tmp)
        os.replace(tmp, KNOWN_GOOD)   # 原子替换，读侧不会看到中间态
        return True


def take_quiet():
    """给 bot.py 用：任何失败都不许影响正常任务。"""
    try:
        return take()
    except Exception as e:
        print(f"[snapshot][warn] take failed: {e}")
        return None


def promote_quiet(sid):
    try:
        return promote(sid)
    except Exception as e:
        print(f"[snapshot][warn] promote failed: {e}")
        return False


def rollback():
    """把 ROOT 恢复到 known-good。返回 (ok, 说明)。

    先把当前现场整份另存 rejected-<ts>/ **再**覆盖——被回滚掉的是事故现场，事后要 diff
    才知道到底哪一轮写了什么，不留档等于把证据一起回滚掉了。
    """
    sid = known_good_id()
    if not sid:
        return False, "没有 known-good 基线（还没有任何一轮任务成功过？）"
    src = os.path.join(SNAP_DIR, sid)
    with _Lock():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        rejected = os.path.join(STORE, f"rejected-{ts}")
        _rsync(ROOT, rejected)
        _rsync(src, ROOT, delete=True)
    return True, f"已回滚到 {sid}，回滚前现场留档于 {rejected}"


def diff_against_known_good():
    """回滚前后都能用：列出当前 ROOT 与基线的差异文件（给告警正文用，看一眼就知道动了啥）。"""
    sid = known_good_id()
    if not sid:
        return []
    cmd = ["rsync", "-rin", "--delete"]
    for e in EXCLUDES:
        cmd += ["--exclude", e]
    cmd += [ROOT.rstrip("/") + "/", os.path.join(SNAP_DIR, sid) + "/"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    lines = []
    for ln in out.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and not parts[1].endswith("/"):
            lines.append(parts[1])
    return lines


def _gc():
    """回收旧快照，保留最近 KEEP 份 + 基线那份。调用方已持锁。"""
    snaps = list_snapshots()
    keep_id = known_good_id()
    for sid in snaps[:-KEEP] if len(snaps) > KEEP else []:
        if sid == keep_id:
            continue
        shutil.rmtree(os.path.join(SNAP_DIR, sid), ignore_errors=True)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "take":
        print(take())
    elif cmd == "promote":
        print("promoted" if promote(argv[2]) else "not promoted (older than current baseline?)")
    elif cmd == "rollback":
        ok, msg = rollback()
        print(msg)
        return 0 if ok else 1
    elif cmd == "diff":
        for f in diff_against_known_good():
            print(f)
    elif cmd == "list":
        kg = known_good_id()
        for sid in list_snapshots():
            print(f"{sid}{'   <- known-good' if sid == kg else ''}")
        if not kg:
            print("(还没有 known-good 基线)")
    else:
        print(__doc__)
        print("usage: snapshot.py [take|promote <id>|rollback|diff|list]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
