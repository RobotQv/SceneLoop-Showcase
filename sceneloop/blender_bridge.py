"""Run this file inside an open Blender to enable SceneLoop Live mode.

Blender's Text Editor: open this file and Run Script. The adapter sends only
data commands through the local project queue; bpy runs on the main timer.
Set SCENELOOP_BRIDGE_DIR before starting Blender to use another queue folder.
SCENELOOP_OUTPUT_ROOT can explicitly bind its project output root; otherwise
only the bridge folder parent is accepted. No project root is auto-whitelisted.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

import bpy

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from blender_worker import execute_request

BRIDGE_DIR = Path(os.environ.get("SCENELOOP_BRIDGE_DIR", SCRIPT_DIR.parent / ".sceneloop" / "bridge")).resolve()
OUTPUT_ROOT = Path(os.environ.get("SCENELOOP_OUTPUT_ROOT") or BRIDGE_DIR.parent).expanduser().resolve()
BRIDGE_DIR.mkdir(parents=True, exist_ok=True)


def _status(busy=False):
    data = {"heartbeat": time.time(), "pid": os.getpid(), "version": bpy.app.version_string, "binary_path": bpy.app.binary_path, "output_root": str(OUTPUT_ROOT), "busy": busy}
    temporary = BRIDGE_DIR / "status.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(BRIDGE_DIR / "status.json")


def _tick():
    _status()
    requests = sorted(BRIDGE_DIR.glob("*.request.json"))
    if not requests:
        return .5
    path = requests[0]
    identifier = path.name.removesuffix(".request.json")
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        if request.get("request_id") != identifier or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("无效请求标识")
        workspace = Path(request["workspace"]).resolve()
        if workspace == OUTPUT_ROOT or not workspace.is_relative_to(OUTPUT_ROOT) or workspace.is_relative_to(BRIDGE_DIR):
            raise ValueError("候选路径必须位于 SceneLoop 输出目录内")
        _status(busy=True)
        result = execute_request(request, live=True)
        response = {"ok": True, "result": result}
    except Exception as exc:
        outcome = getattr(exc, "outcome", None) or ("rejected" if exc.__class__.__name__ == "BlenderError" else "unknown")
        response = {"ok": False, "error": str(exc), "outcome": outcome}
        traceback.print_exc()
    response_path = BRIDGE_DIR / f"{identifier}.response.json"
    temporary = response_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    temporary.replace(response_path)
    path.unlink(missing_ok=True)
    _status()
    return .25


def register():
    previous = bpy.app.driver_namespace.get("sceneloop_bridge_timer")
    if previous and bpy.app.timers.is_registered(previous):
        bpy.app.timers.unregister(previous)
    bpy.app.driver_namespace["sceneloop_bridge_timer"] = _tick
    bpy.app.timers.register(_tick, first_interval=.1, persistent=True)
    _status()
    print(f"SceneLoop bridge connected: {BRIDGE_DIR}")


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    (BRIDGE_DIR / "status.json").unlink(missing_ok=True)


if __name__ == "__main__":
    register()
