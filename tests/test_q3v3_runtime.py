"""The extracted Q3 runtime must really reap owned processes on timeout."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q3v3.runtime import run_command


def alive(pid):
    if os.name == 'nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        try:
            value=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(value))) and value.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        stat=Path(f'/proc/{pid}/stat')
        return not stat.exists() or stat.read_text().split()[2]!='Z'
    except ProcessLookupError:
        return False


class RuntimeTests(unittest.TestCase):
    def test_capture_stdout_stderr_and_status(self):
        result=run_command([sys.executable,'-c','import sys; print("ok"); print("err",file=sys.stderr); sys.exit(7)'],
                           timeout=10,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,7)
        self.assertEqual(result.stdout.strip(),'ok')
        self.assertEqual(result.stderr.strip(),'err')

    def test_timeout_reaps_launcher_and_spawned_child(self):
        with tempfile.TemporaryDirectory() as name:
            marker=Path(name)/'owned_pids.json'
            program='''import subprocess,sys,os,time,json,pathlib
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([sys.executable,'-c',program,str(marker)],timeout=3,text=True)
            self.assertTrue(marker.exists(),'helper did not start before timeout')
            pids=json.loads(marker.read_text())
            deadline=time.perf_counter()+2
            while any(alive(pid) for pid in pids) and time.perf_counter()<deadline:
                time.sleep(.02)
            self.assertFalse(any(alive(pid) for pid in pids))


if __name__=='__main__':
    unittest.main()
