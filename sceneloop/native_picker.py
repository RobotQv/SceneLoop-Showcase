"""Native folder/scene picker executed in an isolated child GUI process.

Only the Python standard library is used. The child process shows the real
operating-system dialog; the parent service never executes GUI code itself.

Resource policy (see GEO task UI-001): the Tcl/Tk resource directories are
derived from the *current* interpreter's own install (``sys.base_prefix`` plus
the version linked into ``_tkinter``). Matching directories are only supplied
to the picker child process when the default resolution fails, never written
to persistent environment variables, and never reused for other subprocesses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from .runtime import AppError

_PICKER_LOCK = threading.Lock()

_PICKER_SCRIPT = """
import json
import sys


def _fail(code):
    print(json.dumps({"error": code}, ensure_ascii=False), flush=True)
    sys.exit(1)


try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    _fail("picker_unavailable")
sys.stdout.reconfigure(encoding="utf-8")
try:
    root = tk.Tk()
except Exception:
    _fail("tcl_init_failed")
root.withdraw()
root.attributes("-topmost", True)
try:
    if sys.argv[1] == "folder":
        value = filedialog.askdirectory(parent=root, title="SceneLoop: Select project folder", mustexist=True)
    elif sys.argv[1] == "scene":
        value = filedialog.askopenfilename(parent=root, title="SceneLoop: Select Blender scene",
                                           filetypes=[("Blender scene", "*.blend")])
    else:
        _fail("picker_unavailable")
    print(json.dumps({"path": value or None}, ensure_ascii=False), flush=True)
except Exception:
    _fail("picker_unavailable")
finally:
    root.destroy()
"""


def linked_tk_versions():
    """Return ((tcl_major, tcl_minor), (tk_major, tk_minor)) linked into this interpreter."""
    try:
        import _tkinter
    except Exception:
        return None
    parsed = []
    for value in (_tkinter.TCL_VERSION, _tkinter.TK_VERSION):
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            parsed.append((int(value[0]), int(value[1])))
        elif isinstance(value, float):
            parsed.append((int(value), int(round((value - int(value)) * 10))))
        elif isinstance(value, str) and "." in value:
            head, _, tail = value.partition(".")
            parsed.append((int(head), int("".join(ch for ch in tail if ch.isdigit()) or 0)))
        else:
            return None
    return tuple(parsed)


def resource_directories(base_prefix, versions):
    """Exact same-install resource directories; no disk scan, no cross-install mixing.

    Returns ``{"TCL_LIBRARY": ..., "TK_LIBRARY": ...}`` only when both matched
    library files exist under the given base prefix, otherwise None.
    """
    if versions is None:
        return None
    from pathlib import Path
    (tcl_major, tcl_minor), (tk_major, tk_minor) = versions
    tcl_directory = Path(base_prefix) / "tcl" / f"tcl{tcl_major}.{tcl_minor}"
    tk_directory = Path(base_prefix) / "tcl" / f"tk{tk_major}.{tk_minor}"
    if (tcl_directory / "init.tcl").is_file() and (tk_directory / "tk.tcl").is_file():
        return {"TCL_LIBRARY": str(tcl_directory), "TK_LIBRARY": str(tk_directory)}
    return None


def child_environment_overrides():
    """Child-only Tcl/Tk resource overrides, or None when not needed/allowed.

    Returns None when the operator already configured TCL_LIBRARY/TK_LIBRARY,
    when versions cannot be determined, or when no matching same-install
    resources exist. The service process environment is never modified here.
    """
    if os.environ.get("TCL_LIBRARY") or os.environ.get("TK_LIBRARY"):
        return None
    return resource_directories(sys.base_prefix, linked_tk_versions())


def _first_report(text):
    """Parse the last structured JSON report line; tolerate library chatter."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if isinstance(report, dict):
            return report
    return None


def _child_report(kind, extra_env):
    """Run one picker child; no shell, no timeout, fixed operation only.

    Returns ``(report, returncode)``. ``returncode`` is None only when the
    child process could not be started (OSError); callers must treat that as
    unavailable without retrying.
    """
    environment = None if extra_env is None else {**os.environ, **extra_env}
    try:
        result = subprocess.run([sys.executable, "-c", _PICKER_SCRIPT, kind],
                                capture_output=True, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                **({"env": environment} if environment is not None else {}))
    except OSError:
        return ({"error": "picker_unavailable"}, None)
    return (_first_report(result.stdout) or {"error": "picker_unavailable"}, result.returncode)


def _is_clean_tcl_init_failure(report, returncode):
    """True only for a pre-window init failure that may use the safe fallback.

    Requires a non-zero exit plus a pure ``tcl_init_failed`` report (no path
    key, so a window may not have opened). Any other failure, success, mixed
    error/path report, or spawn failure never retries.
    """
    return (returncode not in (0, None)
            and isinstance(report, dict)
            and report.get("error") == "tcl_init_failed"
            and "path" not in report)


def _unavailable():
    return AppError("原生文件选择窗口不可用，请手动填写完整路径。", "picker_unavailable", 503)


def pick_native_path(kind):
    """An explicit user click opens a child-process GUI; other HTTP stays usable."""
    if kind not in ("folder", "scene"):
        raise AppError("选择器类型无效。")
    if not _PICKER_LOCK.acquire(blocking=False):
        raise AppError("已有文件选择窗口等待操作，请先完成或取消。", "picker_busy", 409)
    try:
        # No shell or directory enumeration. No deadline treats a user's thinking
        # time as cancellation; this HTTP request waits for the native dialog.
        # Default resolution first, so a working setup or valid operator config
        # is never disturbed by our guesses.
        report, returncode = _child_report(kind, None)
        if _is_clean_tcl_init_failure(report, returncode):
            # The first attempt opened no window: Tk itself could not start, so a
            # single retry with same-install resources cannot duplicate a dialog.
            overrides = child_environment_overrides()
            if overrides:
                report, returncode = _child_report(kind, overrides)
        if returncode != 0:
            # Abnormal exits never count as success or cancel, even when stdout
            # carries a plausible path/null payload. This also covers spawn
            # failures (returncode None) and failed init fallbacks.
            raise _unavailable()
        if not isinstance(report, dict):
            raise _unavailable()
        if "error" in report:
            # Pure errors and conflicting error/path reports are both rejected.
            raise _unavailable()
        if "path" not in report:
            raise _unavailable()
        path = report["path"]
        if path is not None and (not isinstance(path, str) or len(path) > 4096):
            raise _unavailable()
        return {"path": path}
    except AppError:
        raise
    except Exception:
        raise _unavailable() from None
    finally:
        _PICKER_LOCK.release()
