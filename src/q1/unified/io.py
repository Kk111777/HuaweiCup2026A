"""Process-isolated official evaluation and atomic run artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from time import perf_counter

from .budget import run_command

ROOT = Path(__file__).resolve().parents[3]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    serialized=json.dumps(value, ensure_ascii=False, indent=2)+'\n'
    delays=(.02,.05,.15,.3,.6)
    for attempt in range(len(delays)+1):
        try:
            tmp.write_text(serialized, encoding='utf-8')
            tmp.replace(path)
            return
        except OSError:
            # Same bounded Windows sharing-violation recovery as the original V3
            # writer. Persistent failures still propagate and cannot mark success.
            if attempt==len(delays):
                raise
            time.sleep(delays[attempt])
