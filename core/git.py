"""只读查询工作区，不建立同步基线、不修改 git 索引。"""
import subprocess


def local_changes(cwd):
    result = subprocess.run(["git", "--no-optional-locks", "status", "--short"],
                            cwd=cwd, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "不是 git 工作区")
    return {"cwd": cwd, "changes": result.stdout[:20000],
            "truncated": len(result.stdout) > 20000}
