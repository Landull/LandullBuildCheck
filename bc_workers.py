"""
Benchmark and stress workers. Each runs in its own process (the app re-launches itself with
"--worker <name> ..."), prints progress lines, and ends with one line: RESULT {json}.

  cpu  <seconds>             compression throughput on one core (run N copies for multi-core)
  burn <seconds>             CPU stress: compress + verify in a loop; reports any wrong result
  mem                        memory copy bandwidth
  disk <folder> <MB>         sequential write/read and 4K random reads, bypassing the OS cache on Windows
"""

import ctypes
import hashlib
import json
import os
import random
import sys
import time
import zlib

IS_WIN = sys.platform == "win32"


def emit(obj):
    print("RESULT " + json.dumps(obj), flush=True)


def _corpus(size=1 << 20, seed=1234):
    """Text-like data that compresses about 3:1, like real files."""
    rnd = random.Random(seed)
    words = [bytes(rnd.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(rnd.randint(2, 10))) for _ in range(4000)]
    out = bytearray()
    while len(out) < size:
        out += rnd.choice(words) + (b" " if rnd.random() > 0.1 else b"\n")
        if rnd.random() < 0.02:
            out += bytes(rnd.getrandbits(8) for _ in range(rnd.randint(4, 64)))
    return bytes(out[:size])


# ------------------------------------------------------------------ cpu
def cpu_bench(seconds):
    data = _corpus()
    zlib.compress(data, 6)                             # warm up
    done = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        zlib.compress(data, 6)
        hashlib.sha256(data).digest()
        done += len(data)
    dt = time.perf_counter() - t0
    emit({"mbps": done / dt / 1e6, "secs": dt})


def burn(seconds):
    """Keep one core busy and check every result. A mismatch means the CPU or RAM made an error."""
    data = _corpus(seed=os.getpid())
    good = zlib.compress(data, 9)
    digest = hashlib.sha512(data).digest()
    t0 = time.perf_counter()
    last = t0
    rounds = errors = 0
    while time.perf_counter() - t0 < seconds:
        c = zlib.compress(data, 9)
        if c != good or zlib.decompress(c) != data or hashlib.sha512(data).digest() != digest:
            errors += 1
            print(f"ERROR round {rounds}: result didn't match", flush=True)
        x = 1.0001
        for i in range(20000):                         # some floating point work too
            x = (x * x + i) % 1e6
        rounds += 1
        if time.perf_counter() - last >= 5:
            last = time.perf_counter()
            print(f"OK {rounds}", flush=True)
    emit({"rounds": rounds, "errors": errors, "secs": time.perf_counter() - t0})


# ------------------------------------------------------------------ memory
def mem_bench(seconds=3.0):
    size = 256 << 20
    try:
        a = ctypes.create_string_buffer(size)
        b = ctypes.create_string_buffer(size)
    except MemoryError:
        size = 64 << 20
        a = ctypes.create_string_buffer(size)
        b = ctypes.create_string_buffer(size)
    ctypes.memset(a, 0x5A, size)
    ctypes.memset(b, 0, size)
    ctypes.memmove(b, a, size)                         # warm up / fault in pages
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        ctypes.memmove(b, a, size)
        n += 1
    dt = time.perf_counter() - t0
    emit({"gbps": n * size / dt / 1e9, "secs": dt})


# ------------------------------------------------------------------ disk
class _WinFile:
    """Unbuffered file I/O through the Win32 API, so results reflect the drive and not RAM."""
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    NO_BUFFERING, WRITE_THROUGH, SEQUENTIAL, RANDOM = 0x20000000, 0x80000000, 0x08000000, 0x10000000

    def __init__(self):
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k.VirtualAlloc.restype = ctypes.c_void_p
        k.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        self.k = k
        self.wt = wintypes

    def open(self, path, write, flags):
        h = self.k.CreateFileW(path, self.GENERIC_WRITE if write else self.GENERIC_READ, 1, None,
                               2 if write else 3, flags, None)        # CREATE_ALWAYS / OPEN_EXISTING
        if h is None or h == ctypes.c_void_p(-1).value:
            raise OSError(f"CreateFile failed ({ctypes.get_last_error()})")
        return h

    def alloc(self, n):
        return self.k.VirtualAlloc(None, n, 0x3000, 0x04)

    def free(self, p):
        self.k.VirtualFree(p, 0, 0x8000)


def disk_bench(folder, size_mb):
    size_mb = max(256, min(int(size_mb), 8192))
    block = 8 << 20
    total = size_mb << 20
    path = os.path.join(folder, f".buildcheck_{os.getpid()}.tmp")
    res = {"folder": folder, "size_mb": size_mb, "cached": not IS_WIN}
    try:
        if IS_WIN:
            w = _WinFile()
            buf = w.alloc(block)
            rnd = os.urandom(block)
            ctypes.memmove(buf, rnd, block)
            n = w.wt.DWORD()
            # sequential write
            h = w.open(path, True, w.NO_BUFFERING | w.WRITE_THROUGH)
            t0 = time.perf_counter()
            for i in range(total // block):
                if not w.k.WriteFile(h, buf, block, ctypes.byref(n), None):
                    raise OSError(f"WriteFile failed ({ctypes.get_last_error()})")
                if i % 16 == 0:
                    print(f"write {i * 8} MB", flush=True)
            w.k.FlushFileBuffers(h)
            res["write_mbps"] = total / (time.perf_counter() - t0) / 1e6
            w.k.CloseHandle(h)
            # sequential read
            h = w.open(path, False, w.NO_BUFFERING | w.SEQUENTIAL)
            t0 = time.perf_counter()
            for _ in range(total // block):
                if not w.k.ReadFile(h, buf, block, ctypes.byref(n), None):
                    raise OSError(f"ReadFile failed ({ctypes.get_last_error()})")
            res["read_mbps"] = total / (time.perf_counter() - t0) / 1e6
            w.k.CloseHandle(h)
            print("random 4K reads", flush=True)
            # random 4K reads, queue depth 1
            h = w.open(path, False, w.NO_BUFFERING | w.RANDOM)
            blocks = total // 4096
            rng = random.Random(7)
            ops = 0
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 4.0:
                w.k.SetFilePointerEx(h, rng.randrange(blocks) * 4096, None, 0)
                w.k.ReadFile(h, buf, 4096, ctypes.byref(n), None)
                ops += 1
            dt = time.perf_counter() - t0
            res["rand4k_iops"] = ops / dt
            res["rand4k_mbps"] = ops * 4096 / dt / 1e6
            w.k.CloseHandle(h)
            w.free(buf)
        else:
            data = os.urandom(block)
            t0 = time.perf_counter()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            for i in range(total // block):
                os.write(fd, data)
                if i % 16 == 0:
                    print(f"write {i * 8} MB", flush=True)
            os.fsync(fd)
            os.close(fd)
            res["write_mbps"] = total / (time.perf_counter() - t0) / 1e6
            t0 = time.perf_counter()
            with open(path, "rb", buffering=0) as fh:
                while fh.read(block):
                    pass
            res["read_mbps"] = total / (time.perf_counter() - t0) / 1e6
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    emit(res)


def main(args):
    if not args:
        print("usage: --worker cpu|burn|mem|disk ...", file=sys.stderr)
        return 2
    name = args[0]
    try:
        if name == "cpu":
            cpu_bench(float(args[1]) if len(args) > 1 else 8)
        elif name == "burn":
            burn(float(args[1]) if len(args) > 1 else 60)
        elif name == "mem":
            mem_bench()
        elif name == "disk":
            disk_bench(args[1], args[2] if len(args) > 2 else 1024)
        else:
            print(f"unknown worker {name}", file=sys.stderr)
            return 2
    except Exception as e:                             # noqa: BLE001
        print(f"FAILED {e}", flush=True)
        return 1
    return 0
