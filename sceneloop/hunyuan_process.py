"""Local process birth identities, conservative resources and shared GPU admission."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .asset_provider import AssetJobError

GPU_MUTEX = "Local\\SceneLoop-Heavy-GPU-v1"


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path, limit=131072):
    path = Path(path)
    if path.stat().st_size > limit:
        raise AssetJobError("local_record_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def _kernel():
    from ctypes import wintypes as W
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    kernel.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    kernel.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    return kernel, W


def _facts(kernel, W, handle, pid):
    times = [W.FILETIME() for _ in range(4)]
    size = W.DWORD(32768)
    path = ctypes.create_unicode_buffer(size.value)
    exit_code = W.DWORD()
    if (not kernel.GetProcessTimes(handle, *[ctypes.byref(item) for item in times])
            or not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
            or not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))):
        raise AssetJobError("local_process_identity_unavailable")
    return {"pid": pid, "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "executable": str(Path(path.value).resolve()), "alive": exit_code.value == 259}


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise AssetJobError("local_process_identity_unavailable")
    if os.name == "nt":
        kernel, W = _kernel()
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence of exit.
            if ctypes.get_last_error() == 87:
                return None
            raise AssetJobError("local_process_identity_unavailable")
        try:
            return _facts(kernel, W, handle, pid)
        finally:
            kernel.CloseHandle(handle)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        tail = raw[raw.rfind(")") + 2:].split()
        return {"pid": pid, "created": tail[19], "executable": str(Path(f"/proc/{pid}/exe").resolve()),
                "alive": tail[0] != "Z"}
    except FileNotFoundError:
        return None
    except OSError:
        raise AssetJobError("local_process_identity_unavailable") from None


def same_process(actual, expected):
    return actual is not None and all(actual.get(key) == expected.get(key)
                                      for key in ("pid", "created", "executable"))


def parent_pid(pid):
    if os.name != 'nt':
        raw=Path(f'/proc/{pid}/stat').read_text()
        return int(raw[raw.rfind(')')+2:].split()[1])
    from ctypes import wintypes as W
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',W.DWORD),('cntUsage',W.DWORD),('pid',W.DWORD),
            ('heap',ctypes.c_size_t),('module',W.DWORD),('threads',W.DWORD),
            ('parent',W.DWORD),('priority',W.LONG),('flags',W.DWORD),('exe',W.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
    kernel.Process32FirstW.argtypes=kernel.Process32NextW.argtypes=[W.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle in (None,ctypes.c_void_p(-1).value):
        raise AssetJobError('local_process_identity_unavailable')
    try:
        row=Entry();row.dwSize=ctypes.sizeof(row)
        valid=kernel.Process32FirstW(handle,ctypes.byref(row))
        while valid:
            if row.pid==pid:return row.parent
            valid=kernel.Process32NextW(handle,ctypes.byref(row))
        raise AssetJobError('local_process_identity_unavailable')
    finally:kernel.CloseHandle(handle)


def runner_birth():
    """Record actual runner and live ancestry before any model/GPU work."""
    process=process_identity(os.getpid())
    ancestors=[];pid=process['pid'];seen={pid}
    for _ in range(8):
        try:
            parent=parent_pid(pid)
            if not parent or parent in seen:break
            fact=process_identity(parent)
            if not fact:break
            ancestors.append(fact);seen.add(parent);pid=parent
        except (AssetJobError,OSError):break
    return {'process':process,'ancestors':ancestors}


def terminate_original(expected):
    """Validate and terminate through the SAME OS handle, never a bare PID."""
    if os.name != "nt":
        # No safe pidfd fallback on unsupported kernels.
        if not hasattr(os, "pidfd_open"):
            raise AssetJobError("local_cancel_identity_unavailable")
        import signal
        descriptor = os.pidfd_open(expected["pid"])
        try:
            if not same_process(process_identity(expected["pid"]), expected):
                raise AssetJobError("local_process_identity_conflict")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            import select
            poll=select.poll();poll.register(descriptor,select.POLLIN)
            if not poll.poll(5000):
                raise AssetJobError('local_cancel_outcome_unknown')
        finally:
            os.close(descriptor)
        return
    kernel, W = _kernel()
    handle = kernel.OpenProcess(0x1000 | 1 | 0x100000, False, expected["pid"])
    if not handle:
        if ctypes.get_last_error()==87:
            return  # No process owns the original PID now; nothing is killed.
        raise AssetJobError("local_cancel_identity_unavailable")
    try:
        def already_exited():
            times=[W.FILETIME() for _ in range(4)];code=W.DWORD()
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(item) for item in times]):return False
            birth=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
            if birth!=expected['created']:raise AssetJobError('local_process_identity_conflict')
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value!=259
        if already_exited():return
        if not same_process(_facts(kernel, W, handle, expected["pid"]), expected):
            raise AssetJobError("local_process_identity_conflict")
        if not kernel.TerminateProcess(handle, 125):
            if not already_exited():raise AssetJobError("local_cancel_failed")
        kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        if kernel.WaitForSingleObject(handle, 5000) != 0:
            raise AssetJobError("local_cancel_outcome_unknown")
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def gpu_slot():
    """Shared with controlled Blender GPU work; failure never kills another owner."""
    if os.name != "nt":
        raise AssetJobError("local_gpu_lock_platform_unsupported")
    from ctypes import wintypes as W
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [W.LPVOID, W.BOOL, W.LPCWSTR]
    kernel.CreateMutexW.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.ReleaseMutex.argtypes = [W.HANDLE]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateMutexW(None, False, GPU_MUTEX)
    if not handle:
        raise AssetJobError("local_gpu_lock_unavailable")
    owned = False
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state not in (0, 128):
            raise AssetJobError("local_gpu_busy")
        owned = True
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def resources():
    """No torch/model imports. Missing monitor data remains explicitly unavailable."""
    result = {"time": time.time(), "ram": None, "commit": None, "pagefile": None, "gpu": None}
    if os.name == "nt":
        from ctypes import wintypes as W
        class Memory(ctypes.Structure):
            _fields_ = [("length", W.DWORD), ("load", W.DWORD)] + [(key, ctypes.c_ulonglong) for key in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        class Performance(ctypes.Structure):
            _fields_ = [("cb", W.DWORD)] + [(key, ctypes.c_size_t) for key in
                ("commit_total", "commit_limit", "commit_peak", "physical_total", "physical_available",
                 "system_cache", "kernel_total", "kernel_paged", "kernel_nonpaged", "page_size")] + [
                ("handles", W.DWORD), ("processes", W.DWORD), ("threads", W.DWORD)]
        try:
            item = Memory()
            item.length = ctypes.sizeof(item)
            if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(item)):
                raise OSError()
            result["ram"] = {"total": item.total, "available": item.available}
            result["pagefile"] = {"commit_backing_total": item.page_total, "available": item.page_available,
                                  "meaning": "GlobalMemoryStatusEx commit backing, not disk pagefile allocation"}
            performance = Performance()
            performance.cb = ctypes.sizeof(performance)
            if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(performance), performance.cb):
                raise OSError()
            result["commit"] = {key: getattr(performance, key) * performance.page_size
                                for key in ("commit_total", "commit_limit", "commit_peak")}
        except (OSError, AttributeError):
            result["memory_monitor_error"] = "unavailable"
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000 if os.name == "nt" else 0)
        if gpu.returncode == 0 and len(gpu.stdout) < 8192:
            result["gpu"] = {"memory_mib": gpu.stdout.strip(), "processes": None,
                             "limitation": "WDDM may omit process memory; shared lock coordinates cooperative work only"}
            try:
                apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                                       "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                                      creationflags=0x08000000 if os.name == "nt" else 0)
                if apps.returncode == 0 and len(apps.stdout) < 8192:
                    result['gpu']['processes'] = apps.stdout.strip()
                else:
                    result['gpu']['process_monitor_error'] = 'unavailable'
            except (OSError, subprocess.SubprocessError):
                result['gpu']['process_monitor_error'] = 'unavailable'
    except (OSError, subprocess.SubprocessError):
        result["gpu_monitor_error"] = "unavailable"
    return result


def classify(error):
    text = str(error).lower()
    if "cuda" in text and ("out of memory" in text or "outofmemory" in text):
        return "cuda_oom"
    if isinstance(error, MemoryError) or any(key in text for key in ("defaultcpuallocator", "bad_alloc", "cannot allocate memory")):
        return "cpu_memory_failure"
    if any(key in text for key in ("winerror 1450", "winerror 8", "winerror 14")):
        return "system_resource_failure"
    return "local_generation_failed"
