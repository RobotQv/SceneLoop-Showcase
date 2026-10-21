"""Read-only Working snapshots opened in an independent Blender presentation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from .blender_adapter import discover_blender
from .project_runtime import file_sha256, is_link


SCRIPT = Path(__file__).with_name("blender_walkthrough.py").resolve()
CONTROLS = "在 Blender 的 3D 视口按 F3，搜索 Walk Navigation（步行导航）；鼠标转向，WASD 移动，Q/E 升降，滚轮调速，Esc 退出。"


def _error(message, code="walkthrough_failed", status=409):
    from .runtime import AppError
    return AppError(message, code, status)


def _project(manager, identifier, body):
    project = manager._projects().get(identifier, verify_artifacts=False)
    if (project["revision"] != body["expected_project_revision"]
            or project["workspace"]["working_version"] != body["expected_working_version"]):
        raise _error("当前工作版已变化，请刷新后重新打开漫游。", "walkthrough_version_conflict")
    if not project.get("current_scene"):
        raise _error("项目还没有正式 Working Scene，先建场或导入。", "walkthrough_scene_missing")
    return project


def _plain_tree(path, base):
    # Never follow an existing junction/symlink into another scene or evidence directory.
    if not path.is_relative_to(base) or any(is_link(item) for item in (base, path, *path.parents) if item == base or item.is_relative_to(base)):
        raise _error("漫游文件目录指向不安全，未打开 Blender。", "unsafe_walkthrough_path")


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _failed_record(path, record, base):
    from .runtime import AppError
    try:
        _plain_tree(path, base)
        _write(path, {**record, "status": "failed"})
    except (OSError, AppError):
        pass  # Keep the original preparation error when auxiliary metadata cannot be saved.


def open_walkthrough(manager, identifier, body):
    """Freeze the authoritative Working version; never accept client paths or launch arguments."""
    fields = {"expected_project_revision", "expected_working_version"}
    if (not isinstance(body, dict) or set(body) != fields
            or any(type(body[key]) is not int or body[key] < 0 for key in fields)):
        raise _error("漫游请求只接受当前项目 revision 和 Working Version 整数。", "invalid_walkthrough_request", 400)
    with manager.lock:
        busy = getattr(manager, "_walkthrough_busy", None)
        if busy is None:
            busy = manager._walkthrough_busy = set()
        if identifier in busy:
            raise _error("本项目的漫游窗口正在准备，请稍候。", "walkthrough_busy")
        project = _project(manager, identifier, body)
        selection = copy.deepcopy(project["current_scene"])
        version = manager.scene_version(selection["run_id"], selection.get("revision_id"), allow_active=True)
        if version["run"].get("project_id") != identifier:
            raise _error("当前场景不属于此项目。", "unsafe_walkthrough_source")
        files = {kind: Path(path) for kind, path in version["files"].items()}
        hashes = {kind: file_sha256(path) for kind, path in files.items()}
        base = manager._projects().runs_root(identifier).parent.resolve()
        root = base / "walkthroughs"
        _plain_tree(root, base)
        root.mkdir(exist_ok=True)
        presentation = root / uuid.uuid4().hex
        _plain_tree(presentation, base)
        presentation.mkdir()
        busy.add(identifier)
    record = {"schema_version": 1, "id": presentation.name, "project_id": identifier,
              "source": {"run_id": selection["run_id"], "revision_id": selection.get("revision_id"),
                         "working_version": body["expected_working_version"], "project_revision": body["expected_project_revision"],
                         "files": {key: {"path": str(value), "sha256": hashes[key]} for key, value in files.items()}},
              "display_path": str(presentation / "scene.blend"), "status": "preparing"}
    manifest = presentation / "walkthrough.json"
    try:
        discovery = discover_blender()
        if not discovery.get("available"):
            raise _error("未找到可运行的 Blender，请设置 BLENDER_PATH 后重启服务。", "walkthrough_blender_unavailable", 503)
        for kind, source in files.items():
            _plain_tree(source, manager.run_directory(selection["run_id"]))
            target = presentation / {"blend": "scene.blend", "plan": "plan.json", "inspection": "inspection.json"}[kind]
            shutil.copyfile(source, target)
            if file_sha256(source) != hashes[kind] or file_sha256(target) != hashes[kind]:
                raise _error("场景文件在复制期间发生变化，未打开 Blender。", "revision_changed")
        _write(manifest, record)
        # Open the source only in a private background process so relative libraries/images
        # resolve at their original location, then save absolute references into the copy.
        prepare = [discovery["path"], "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(SCRIPT), "--", str(manifest), "--prepare"]
        with (presentation / "prepare.log").open("wb") as log:
            result = subprocess.run(prepare, stdout=log, stderr=subprocess.STDOUT, timeout=300,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        report_path = presentation / "prepared.json"
        if result.returncode or not report_path.is_file():
            raise _error("Blender 未能准备展示副本；原场景保持，详见本地漫游 prepare.log。", "walkthrough_prepare_failed", 503)
        prepared = json.loads(report_path.read_text(encoding="utf-8"))
        display = presentation / "scene.blend"
        if prepared.get("id") != record["id"] or prepared.get("display_sha256") != file_sha256(display):
            raise _error("展示副本身份或文件不完整，未打开 Blender。", "walkthrough_integrity_failed")
        with manager.lock:
            latest = _project(manager, identifier, body)
            if latest["current_scene"] != selection:
                raise _error("当前场景在准备期间变化，请刷新后重试。", "walkthrough_version_conflict")
            manager.scene_version(selection["run_id"], selection.get("revision_id"), allow_active=True)
            for kind, path in files.items():
                if file_sha256(path) != hashes[kind]:
                    raise _error("来源场景在准备期间变化，未打开 Blender。", "revision_changed")
            _plain_tree(display, base)
            record.update(status="prepared", display_sha256=prepared["display_sha256"])
            _write(manifest, record)
            argv = [discovery["path"], "--factory-startup", "--disable-autoexec", str(display),
                    "--python", str(SCRIPT), "--", str(manifest), "--display"]
            with (presentation / "display.log").open("ab") as log:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           close_fds=True, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if process.poll() is not None:
                raise _error("Blender 展示进程已退出，请检查本地 display.log。", "walkthrough_launch_failed", 503)
            record.update(status="launched", pid=process.pid)
            try:
                _write(manifest, record)
            except OSError:
                pass  # A launched window must not be reported as failed because its log write failed.
        return {"walkthrough_id": record["id"], "source": {key: record["source"][key] for key in
                ("run_id", "revision_id", "working_version", "project_revision")},
                "launch_status": "launched", "walk_status": "manual_start_required", "controls": CONTROLS,
                "message": f"已启动 Blender 展示窗口 · Working Version {body['expected_working_version']}。{CONTROLS} 此副本不会回写 Working。"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        _failed_record(manifest, record, base)
        raise _error("漫游准备或启动失败，原场景保持；请检查本地 Blender 与漫游日志。", "walkthrough_launch_failed", 503) from error
    except Exception:
        _failed_record(manifest, record, base)
        raise
    finally:
        with manager.lock:
            busy.discard(identifier)
