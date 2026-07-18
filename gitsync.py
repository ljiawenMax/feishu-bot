"""增量同步：把某个 chat 的工作目录改动，按「上次同步以来」生成增量 git bundle。

设计目标：远程项目与本机同版本、只被动接收同步文件，远程 `git fetch` 该 bundle 后
`merge --ff-only`（或 /resync 全量 bundle 后 `reset --hard`）即可拉平。

之前用 `git diff` 生成纯文本 `.patch`，靠 `git apply` 按上下文行匹配应用——一旦本地
基线 ref 与远程实际状态出现任何偏差（例如远程曾经历过一次脱离 gitsync 的手工整体
覆盖），后续每一轮的上下文都会跟着错位，且 `git apply` 只会给出含糊的 "does not
apply"，很难判断到底错在哪一轮。改用 bundle 后，同步单位是真实的 git 对象而非文本
上下文：`fetch` 要么完整拿到对象要么报错，`merge --ff-only` 要么成功快进要么明确
报 "not possible to fast-forward"——一旦基线又偏了，远程会立刻收到清晰信号，此时
发 `/resync` 换一个不依赖增量基线、以本机当前状态为准强制拉平的全量 bundle 即可
恢复，不需要人工排查是哪一轮 patch 断的。

- 每个 chat_id 维护一个隐藏基线 ref `refs/feishu-sync/<chat_id>`，指向「上次已
  发送状态」的快照提交（游离对象，不在任何分支上）。
- 生成同步文件时：把当前工作树快照成一个临时提交（用独立的临时 index，**不碰用户的
  HEAD / 分支 / 提交历史 / 暂存区**），与基线之间的差异非空则打包发送并把基线推进
  到该快照；空则跳过。
- 因此本机 commit 与否都不影响：快照抓的是工作树内容，而非 HEAD。
- 快照走 `git add -A`（临时 index），自动尊重 .gitignore：被忽略的文件（.venv、
  __pycache__、.env.local 等）不会进同步文件。
- bundle 需要一个可被 `git fetch` 按名字引用的 ref 才能生成，因此额外维护一个
  临时的「快照 tip」ref `refs/feishu-sync-tip/<chat_id>`，每轮生成时指向本轮快照，
  随 bundle 一起发送 ref 名供远程 fetch 用（ref 名字固定不变，远程命令每轮无需改）。
"""

import os
import re
import subprocess
import tempfile

REF_PREFIX = "refs/feishu-sync/"
TIP_REF_PREFIX = "refs/feishu-sync-tip/"
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


def _tip_ref(chat_id):
    return TIP_REF_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", chat_id)


def _base_commit(work_dir, chat_id):
    """取增量基线 commit：优先 sync 基线 ref，其次当前 HEAD；
    仓库刚 git init、尚无任何提交（HEAD 未诞生）时返回 ""，交由调用方走全量。"""
    base = _git(work_dir, "rev-parse", "--verify", "--quiet", _ref(chat_id), check=False).strip()
    if base:
        return base
    # --verify --quiet：HEAD 不存在时返回空串而非报 "ambiguous argument 'HEAD'"
    return _git(work_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()


def _snapshot_commit(work_dir, base):
    """把当前工作树快照成一个游离提交，返回其 sha。base 为父提交；base 为空
    （仓库刚 git init、尚无任何提交）时生成无父的 root 提交，全量纳入工作树。
    用临时 GIT_INDEX_FILE，绝不触碰用户真实 index / 分支。"""
    fd, idx = tempfile.mkstemp(prefix="feishu-sync-idx-")
    os.close(fd)
    env = {"GIT_INDEX_FILE": idx, **_IDENTITY}
    try:
        if base:
            _git(work_dir, "read-tree", base, env=env)   # 临时 index 以 base 为起点
        else:
            # base 为空（空仓库）：初始化一个合法的空 index（mkstemp 的 0 字节文件 git 不认）
            _git(work_dir, "read-tree", "--empty", env=env)
        _git(work_dir, "add", "-A", env=env)          # 纳入增删改（尊重 .gitignore）
        tree = _git(work_dir, "write-tree", env=env).strip()
        parent = ["-p", base] if base else []          # 无 base 则生成 root 提交
        commit = _git(work_dir, "commit-tree", tree, *parent,
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


def incremental_bundle(work_dir, chat_id, out_path):
    """生成「上次同步以来」的增量 bundle，写入 out_path。

    返回 {"seq": int, "base": str, "snap": str, "tip_ref": str}；无改动时返回 None。
    成功生成后基线 ref 前移到本次快照（即已计入「已发送」），故下轮只含新增改动。
    """
    ref = _ref(chat_id)
    base = _base_commit(work_dir, chat_id)   # 空仓库(无提交)时为 ""

    snap = _snapshot_commit(work_dir, base)
    # 有基线：diff 基线→快照判断有无改动；无基线(空仓库)：看快照里是否有文件
    if base:
        changed = _git(work_dir, "diff", "--stat", base, snap).strip()
    else:
        changed = _git(work_dir, "ls-tree", "-r", "--name-only", snap).strip()
    if not changed:
        return None

    tip_ref = _tip_ref(chat_id)
    _git(work_dir, "update-ref", tip_ref, snap)
    # 有基线打 base..tip 的增量；无基线则打包全部可达历史（首轮=全量）
    rev = f"{base}..{tip_ref}" if base else tip_ref
    _git(work_dir, "bundle", "create", out_path, rev)

    _git(work_dir, "update-ref", ref, snap)   # 基线前移，本次改动计入「已发送」
    seq = _next_seq(work_dir, chat_id)
    return {"seq": seq, "base": (base[:12] if base else "(空仓库)"),
            "snap": snap[:12], "tip_ref": tip_ref}


def full_bundle(work_dir, chat_id, out_path):
    """生成不依赖增量基线的全量 bundle（含快照可达的完整历史），写入 out_path。

    用于 /resync：不管远程此前处于什么状态，远程 fetch 后 `reset --hard` 到这份
    快照即可强制与本机当前工作树拉平，作为「怀疑基线偏了」时的恢复手段。
    成功后同样把基线 ref 前移到本次快照，后续增量同步从这里重新计起。
    """
    ref = _ref(chat_id)
    base = _base_commit(work_dir, chat_id)   # 空仓库(无提交)时为 ""，snapshot 生成 root 提交
    snap = _snapshot_commit(work_dir, base)

    tip_ref = _tip_ref(chat_id)
    _git(work_dir, "update-ref", tip_ref, snap)
    _git(work_dir, "bundle", "create", out_path, tip_ref)   # 不带 base..，打包全部可达历史

    _git(work_dir, "update-ref", ref, snap)
    seq = _next_seq(work_dir, chat_id)
    return {"seq": seq, "snap": snap[:12], "tip_ref": tip_ref}
