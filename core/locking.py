"""同一索引目录的线程/进程互斥；锁文件在目录外，清空缓存也保留锁。"""
import contextlib
import fcntl
import functools
import os
import threading

_locks = {}
_registry_lock = threading.Lock()
_local = threading.local()


@contextlib.contextmanager
def index_lock(dest):
    path = os.path.realpath(os.path.expanduser(os.fspath(dest)))
    with _registry_lock:
        lock = _locks.setdefault(path, threading.RLock())
    with lock:
        held = getattr(_local, 'held', set())
        if path in held:
            yield
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + '.lock', 'a') as handle:
            os.chmod(path + '.lock', 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            _local.held = held
            held.add(path)
            try:
                yield
            finally:
                held.remove(path)
                fcntl.flock(handle, fcntl.LOCK_UN)


def locked_space(position):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            dest = args[position] if len(args) > position else kwargs['dest']
            with index_lock(dest):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
