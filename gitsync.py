"""增量同步：把某个 chat 的工作目录改动**在本地提交**，并按「上次同步以来」生成增量 git bundle。

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

**同步 = 先在本地真提交，再打包真实历史**（旧版是造游离快照、不碰 HEAD，导致本机
`git log` 里查不到任何同步记录、远程收到的也只是一条 message 全一样的匿名快照链，
本机自己的 commit 反而被压平丢失）。现在：

- 工作树有改动时，先在**当前分支**上做一次真实提交（`git add -A` + `git commit`，
  message 为本轮飞书任务内容），因此本机与远程的 `git log` 完全一致、都能看到每轮
  改了什么、是谁触发的。**代价：会写入用户的提交历史，并把暂存区一并提交。**
- 提交走 `--no-verify` 且关掉 gpg 签名：同步不应被 pre-commit hook 或签名配置阻断。
- 用户已配 `user.name/email` 时用用户身份提交，未配才回退 feishu-sync 身份。
- `git add -A` 自动尊重 .gitignore：被忽略的文件（.venv、__pycache__、.env.local 等）
  不会进提交、也就不会进同步文件。
- 每个 chat_id 维护一个隐藏基线 ref `refs/feishu-sync/<chat_id>`，指向「上次已发送
  的提交」；每轮打包 `基线..HEAD` 的真实提交，发完基线前移到 HEAD。
- 基线若不是当前 HEAD 的祖先（本机 rebase / reset / 换分支改写过历史），增量已无意义，
  直接报错要求改发 `/resync` 全量拉平，而不是发出一个远程必然 ff 失败的 bundle。
- bundle 需要一个可被 `git fetch` 按名字引用的 ref，因此额外维护 `refs/feishu-sync-tip/
  <chat_id>` 指向本轮 HEAD，随 bundle 一起发送 ref 名供远程 fetch（名字固定不变，
  远程命令每轮无需改）。
"""

import os
import re
import subprocess

REF_PREFIX = "refs/feishu-sync/"
TIP_REF_PREFIX = "refs/feishu-sync-tip/"
DEFAULT_MESSAGE = "feishu-sync: 同步工作目录改动"
# 用户未配置 user.name/email 时的回退身份（配了则用用户自己的，提交进的是用户历史）
_FALLBACK_IDENTITY = {
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


def _head(work_dir):
    """当前 HEAD 的 sha；仓库刚 git init、尚无任何提交时返回 ""。
    --verify --quiet：HEAD 不存在时给空串而非报 "ambiguous argument 'HEAD'"。"""
    return _git(work_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()


def _identity_env(work_dir):
    """用户已配 user.email 就用用户身份提交（提交进的是用户自己的历史），未配才回退。"""
    email = _git(work_dir, "config", "--get", "user.email", check=False).strip()
    name = _git(work_dir, "config", "--get", "user.name", check=False).strip()
    return {} if (email and name) else dict(_FALLBACK_IDENTITY)


def has_local_changes(work_dir):
    """工作树/暂存区相对 HEAD 是否有改动（含未跟踪文件，尊重 .gitignore）。"""
    return bool(_git(work_dir, "status", "--porcelain").strip())


def commit_worktree(work_dir, message):
    """把当前工作树改动在**当前分支**上真实提交一次，返回新 commit sha；无改动返回 ""。

    `git add -A` 会把用户暂存区一并纳入（方案取舍：同步即提交）。`--no-verify` +
    关闭 gpg 签名，避免 pre-commit hook / 签名配置让同步失败或改动文件导致状态错位。
    空仓库（尚无提交）时生成 root 提交。
    """
    if not has_local_changes(work_dir):
        return ""
    env = _identity_env(work_dir)
    _git(work_dir, "add", "-A", env=env)      # 纳入增删改（尊重 .gitignore）
    _git(work_dir, "-c", "commit.gpgsign=false", "commit", "--no-verify",
         "-m", (message or DEFAULT_MESSAGE), env=env)
    return _head(work_dir)


def _log_subjects(work_dir, base, head, limit=10):
    """列出 base..head（base 为空则取 head 全部历史）的提交摘要，供群消息展示。
    返回 [(sha12, subject), ...]，最多 limit 条。"""
    rev = f"{base}..{head}" if base else head
    out = _git(work_dir, "log", f"-{limit}", "--format=%h %s", rev, check=False)
    items = []
    for line in out.strip().splitlines():
        sha, _, subject = line.partition(" ")
        items.append((sha, subject))
    return items


def _is_ancestor(work_dir, a, b):
    """a 是否是 b 的祖先（`merge-base --is-ancestor` 靠退出码表达，不能看 stdout）。"""
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          cwd=work_dir, capture_output=True).returncode == 0


def _count_commits(work_dir, base, head):
    rev = f"{base}..{head}" if base else head
    out = _git(work_dir, "rev-list", "--count", rev, check=False).strip()
    return int(out) if out.isdigit() else 0


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


def incremental_bundle(work_dir, chat_id, out_path, message=None):
    """先把工作树改动在本地提交（message 为提交说明），再生成「上次同步以来」的增量
    bundle 写入 out_path。

    返回 {"seq", "base", "tip", "tip_ref", "new_commit", "n_commits", "commits"}；
    无任何待同步提交时返回 None。成功后基线 ref 前移到 HEAD，故下轮只含新增提交。
    基线不是 HEAD 祖先（历史被改写）时抛 RuntimeError，提示改用 /resync。
    """
    ref = _ref(chat_id)
    base = _git(work_dir, "rev-parse", "--verify", "--quiet", ref, check=False).strip()

    new_commit = commit_worktree(work_dir, message)
    head = _head(work_dir)
    if not head:
        return None                    # 空仓库且无任何可提交文件
    if base == head:
        return None                    # 没有新提交，无需同步
    if base and not _is_ancestor(work_dir, base, head):
        raise RuntimeError(
            f"同步基线 {base[:12]} 不是当前 HEAD {head[:12]} 的祖先"
            "（本机历史被 rebase/reset/切分支改写过），增量已无法快进。"
            "本轮改动已提交到本地、不会丢，发 /resync 全量拉平即可同步出去。")

    tip_ref = _tip_ref(chat_id)
    _git(work_dir, "update-ref", tip_ref, head)
    # 有基线打 base..tip 的增量；无基线则打包全部可达历史（首轮=全量）
    rev = f"{base}..{tip_ref}" if base else tip_ref
    _git(work_dir, "bundle", "create", out_path, rev)

    commits = _log_subjects(work_dir, base, head)
    n = _count_commits(work_dir, base, head)
    _git(work_dir, "update-ref", ref, head)   # 基线前移，本次提交计入「已发送」
    seq = _next_seq(work_dir, chat_id)
    return {"seq": seq, "base": (base[:12] if base else "(全量首轮)"),
            "tip": head[:12], "tip_ref": tip_ref,
            "new_commit": bool(new_commit), "n_commits": n, "commits": commits}


def full_bundle(work_dir, chat_id, out_path, message=None):
    """先在本地提交工作树改动，再生成不依赖增量基线的全量 bundle（HEAD 可达的完整
    历史），写入 out_path。

    用于 /resync：不管远程此前处于什么状态，远程 fetch 后 `reset --hard` 到本机 HEAD
    即可强制拉平，作为「怀疑基线偏了 / 本机历史被改写」时的恢复手段。
    成功后基线 ref 前移到 HEAD，后续增量同步从这里重新计起。
    """
    ref = _ref(chat_id)
    new_commit = commit_worktree(work_dir, message)
    head = _head(work_dir)
    if not head:
        return None                    # 空仓库且无任何可提交文件

    tip_ref = _tip_ref(chat_id)
    _git(work_dir, "update-ref", tip_ref, head)
    _git(work_dir, "bundle", "create", out_path, tip_ref)   # 不带 base..，打包全部可达历史

    commits = _log_subjects(work_dir, "", head, limit=5)
    _git(work_dir, "update-ref", ref, head)
    seq = _next_seq(work_dir, chat_id)
    return {"seq": seq, "tip": head[:12], "tip_ref": tip_ref,
            "new_commit": bool(new_commit), "commits": commits}
