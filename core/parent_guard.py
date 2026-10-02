"""Linux 子进程入口：父进程退出时杀掉独立进程组里的模型进程。"""
import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), '无法设置父进程退出信号')
    # 父进程可能在启动本入口与 prctl 之间退出。
    if os.getppid() != parent:
        return 125
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
