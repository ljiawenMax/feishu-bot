"""增量补丁：把某个 chat 的工作目录改动，按「上次同步以来」生成增量 diff。

设计目标：远程项目与本机同版本、只被动接收补丁，故每轮只需把「相对上次已发送
状态」的增量 diff 发过去，远程按序 `git apply` 即可逐轮叠加。

- 每个 chat_id 维护一个隐藏基线 ref `refs/feishu-sync/<chat_id>`，指向「上次已
  发送状态」的快照提交（游离对象，不在任何分支上）。
- 生成补丁时：把当前工作树快照成一个临时提交（用独立的临时 index，**不碰用户的
  HEAD / 分支 / 提交历史 / 暂存区**），与基线做 `git diff`，非空则发送并把基线推进
  到该快照；空则跳过。
- 因此本机 commit 与否都不影响：快照抓的是工作树内容，而非 HEAD。
- 快照走 `git add -A`（临时 index），自动尊重 .gitignore：被忽略的文件（.venv、
  __pycache__、.env.local 等）不会进补丁。
"""

import os
import re
import subprocess
import tempfile

REF_PREFIX = "refs/feishu-sync/"
# 快照提交用的固定身份，避免依赖用户的 user.name/email 配置
_IDENTITY = {
    "GIT_AUTHOR_NAME": "feishu-sync",
    "GIT_AUTHOR_EMAIL": "feishu-sync@local",
    "GIT_COMMITTER_NAME": "feishu-sync",
    "GIT_COMMITTER_EMAIL": "feishu-sync@local",
}


def _git(work_dir, *args, env=None, check=True):
    """在 work_dir 里跑 git，返回 stdout。check=False 时失败不抛（用于探测）。"""
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run(["git", *args], cwd=work_dir, env=e,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def is_git_repo(work_dir):
    """work_dir 是否在一个 git 工作树内（且 git 可用）。"""
    try:
        out = _git(work_dir, "rev-parse", "--is-inside-work-tree", check=False)
    except (FileNotFoundError, OSError):
        return False
    return out.strip() == "true"


def _ref(chat_id):
    return REF_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", chat_id)


def _snapshot_commit(work_dir, base):
    """把当前工作树快照成一个游离提交（父为 base），返回其 sha。
    用临时 GIT_INDEX_FILE，绝不触碰用户真实 index / 分支。"""
    fd, idx = tempfile.mkstemp(prefix="feishu-sync-idx-")
    os.close(fd)
    env = {"GIT_INDEX_FILE": idx, **_IDENTITY}
    try:
        _git(work_dir, "read-tree", base, env=env)   # 临时 index 以 base 为起点
        _git(work_dir, "add", "-A", env=env)          # 纳入增删改（尊重 .gitignore）
        tree = _git(work_dir, "write-tree", env=env).strip()
        commit = _git(work_dir, "commit-tree", tree, "-p", base,
                      "-m", "feishu-sync snapshot", env=env).strip()
        return commit
    finally:
        try:
            os.remove(idx)
        except OSError:
            pass


def _next_seq(work_dir, chat_id):
    """每个 chat 一个自增计数器（存 .git 下），作为补丁序号，保证远程按序 apply。"""
    gitdir = _git(work_dir, "rev-parse", "--git-dir").strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(work_dir, gitdir)
    d = os.path.join(gitdir, "feishu_sync")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_]", "_", chat_id)
    fpath = os.path.join(d, f"{safe}.seq")
    n = 0
    if os.path.exists(fpath):
        try:
            n = int((open(fpath).read().strip() or "0"))
        except ValueError:
            n = 0
    n += 1
    with open(fpath, "w") as fh:
        fh.write(str(n))
    return n


def incremental_patch(work_dir, chat_id):
    """生成「上次同步以来」的增量补丁。

    返回 {"patch": str, "seq": int, "base": str, "snap": str}；无改动时返回 None。
    成功生成后基线 ref 前移到本次快照（即已计入「已发送」），故下轮只含新增改动。
    """
    ref = _ref(chat_id)
    base = _git(work_dir, "rev-parse", "--verify", "--quiet", ref, check=False).strip()
    if not base:
        # 首轮：基线取当前 HEAD（远程与本机同版本的那个 commit）
        base = _git(work_dir, "rev-parse", "HEAD").strip()

    snap = _snapshot_commit(work_dir, base)
    # --binary：即便偶有二进制改动也能被 git apply 应用（正常只改文本）
    patch = _git(work_dir, "diff", "--binary", base, snap)
    if not patch.strip():
        return None

    _git(work_dir, "update-ref", ref, snap)   # 基线前移，本次改动计入「已发送」
    seq = _next_seq(work_dir, chat_id)
    return {"patch": patch, "seq": seq, "base": base[:12], "snap": snap[:12]}
