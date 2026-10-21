"""Optional request-local monotonic timing; never records headers or content."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import threading
import time

_current = ContextVar('upload_profile', default=None)


def add(name, value):
    current = _current.get()
    if current is not None:
        current[name] = current.get(name, 0) + value


def identify(**fields):
    current = _current.get()
    if current is not None:
        current.update(fields)


@contextmanager
def measure(name):
    if _current.get() is None:
        yield
        return
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        add(name, (time.perf_counter_ns() - start) // 1000)


@contextmanager
def timed_lock(lock):
    with measure('lock_wait_us'):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def request(sink, method):
    if sink is None:
        yield
        return
    current = {'event': 'upload_profile', 'schema': 1, 'method': method,
               'epoch_ns': time.time_ns()}
    token = _current.set(current)
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        current['elapsed_us'] = (time.perf_counter_ns() - start) // 1000
        _current.reset(token)
        try:
            sink(current)
        except OSError:
            pass  # Measurement failure must not change an upload receipt.


class FileSink:
    """Bounded, exclusive 0600 output. No per-record fsync on the request path."""
    def __init__(self, path, limit=2048):
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.limit = limit
        self.lock = threading.Lock()
        self.failed = False

    def __call__(self, record):
        encoded = (json.dumps(record, separators=(',', ':')) + '\n').encode()
        with self.lock:
            if self.failed or self.limit <= 0:
                return
            self.limit -= 1
            try:
                if os.write(self.fd, encoded) != len(encoded):
                    self.failed = True
            except OSError:
                self.failed = True

    def close(self):
        with self.lock:
            self.failed = True
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
