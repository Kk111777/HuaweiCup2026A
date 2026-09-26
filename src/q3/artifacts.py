"""Atomic Q3 publication with bounded retries for transient Windows file locks."""
import time
from common.artifacts import atomic_text as _atomic_text, write_json as _write_json, file_hash


def retry_locked(function, *args):
    for delay in (0, .02, .05, .1, .2, .4):
        if delay:
            time.sleep(delay)
        try:
            return function(*args)
        except PermissionError:
            if delay == .4:
                raise


def atomic_text(path, value):
    return retry_locked(_atomic_text, path, value)


def write_json(path, value):
    return retry_locked(_write_json, path, value)
