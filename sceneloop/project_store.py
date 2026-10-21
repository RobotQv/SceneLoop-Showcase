"""Persistent workspace identities and explicitly selected scene versions.

The server owns its global data-directory lock. Each opened project additionally
holds its own OS lock, even when servers use different global data directories.
Closing the repository is only safe after every project writer has stopped.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .data_lock import DataDirectoryInUse, DataDirectoryLock
from .project_state_store import ProjectStateStore, StateStoreError

MAX_METADATA_BYTES = 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ProjectError(Exception):
    def __init__(self, message, code="invalid_project", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _plain(path):
    """Reject symlinks and Windows junctions before opening managed metadata."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ProjectError("项目管理目录或文件不能是符号链接或目录联接。", "unsafe_project_path")


def _read(path):
    _plain(path)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate metadata key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Non-finite metadata number")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("Metadata too large")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ProjectError("项目记录无法读取或已损坏；原文件已保留。", "project_storage_error", 500) from None


def _write(path, value):
    _plain(path)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_METADATA_BYTES:
            raise ProjectError("项目记录过大，无法保存。", "project_storage_error", 500)
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _plain(path)
        temporary.replace(path)
    except OSError:
        raise ProjectError("项目记录无法保存；已有文件未被主动清空。", "project_storage_error", 500) from None
    finally:
        if temporary.exists():
            temporary.unlink()


def _name(value, default=None):
    value = default if value is None else value
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or _CONTROL.search(value):
        raise ProjectError("项目名称需要是 1–200 字的文字。")
    return value.strip()


def _selection(value):
    if not isinstance(value, dict) or set(value) != {"run_id", "revision_id", "scene_id", "title"}:
        raise ProjectError("当前场景需要完整的任务、版本、场景 ID 与标题。", "invalid_scene_selection")
    run_id, revision_id = value["run_id"], value["revision_id"]
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise ProjectError("场景任务 ID 无效。", "invalid_scene_selection")
    if revision_id is not None and (not isinstance(revision_id, str) or not re.fullmatch(r"r[0-9]{3,6}", revision_id)):
        raise ProjectError("场景版本 ID 无效。", "invalid_scene_selection")
    for field, maximum in (("scene_id", 128), ("title", 500)):
        item = value[field]
        if not isinstance(item, str) or not item.strip() or len(item) > maximum or _CONTROL.search(item):
            raise ProjectError("场景 ID 或标题无效。", "invalid_scene_selection")
    return copy.deepcopy(value)


def _workspace_state(value, current_scene=None):
    """Validate persistent conversation state while migrating older projects in memory."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ProjectError("项目工作区状态无效；原文件已保留。", "project_storage_error", 500)
    result = {
        "schema_version": 1,
        "working_version": 1 if current_scene else 0,
        "stage": "structure",
        "recovery_limit": 1,
        "recoveries": [],
        "batch_start": None,
        "checkpoints": [],
        "issues": [],
        "turns": [],
        "goal": {"scene_intent": "", "current_request": "", "constraints": {},
                 "updated_at": None, "history": []},
        "rolling_summary": {"text": "", "through_turn": 0, "sources": []},
        "latest_judgement": None,
    }
    result.update(copy.deepcopy(value))
    if result.get("schema_version") != 1 or type(result.get("working_version")) is not int or result["working_version"] < 0:
        raise ProjectError("项目工作版本记录无效；原文件已保留。", "project_storage_error", 500)
    if not isinstance(result.get("stage"), str) or not result["stage"] or len(result["stage"]) > 80:
        raise ProjectError("项目阶段记录无效；原文件已保留。", "project_storage_error", 500)
    if result.get("recovery_limit") not in (1, 2):
        raise ProjectError("项目恢复点数量设置无效；原文件已保留。", "project_storage_error", 500)
    for field, maximum in (("recoveries", 2), ("checkpoints", 100), ("issues", 128), ("turns", 120)):
        rows = result.get(field)
        if not isinstance(rows, list) or len(rows) > maximum or any(not isinstance(row, dict) for row in rows):
            raise ProjectError("项目工作区列表记录无效；原文件已保留。", "project_storage_error", 500)
    for row in result["recoveries"]:
        _selection(row.get("selection"))
    if result.get("batch_start") is not None:
        if not isinstance(result["batch_start"], dict):
            raise ProjectError("项目批次起点记录无效；原文件已保留。", "project_storage_error", 500)
        _selection(result["batch_start"].get("selection"))
    for row in result["checkpoints"]:
        if not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
            raise ProjectError("项目检查点记录无效；原文件已保留。", "project_storage_error", 500)
        _selection(row.get("selection"))
    summary = result.get("rolling_summary")
    if (not isinstance(summary, dict) or not isinstance(summary.get("text", ""), str)
            or type(summary.get("through_turn", 0)) is not int or not isinstance(summary.get("sources", []), list)):
        raise ProjectError("项目滚动摘要无效；原文件已保留。", "project_storage_error", 500)
    goal = result.get("goal")
    if (not isinstance(goal, dict) or not isinstance(goal.get("scene_intent", ""), str)
            or not isinstance(goal.get("current_request", ""), str)
            or not isinstance(goal.get("constraints", {}), dict)
            or not isinstance(goal.get("history", []), list) or len(goal.get("history", [])) > 40
            or any(not isinstance(row, dict) for row in goal.get("history", []))):
        raise ProjectError("项目目标记录无效；原文件已保留。", "project_storage_error", 500)
    return result


def _manifest(value, expected_id=None):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("id"), str) or not _ID.fullmatch(value["id"])
            or (expected_id is not None and value["id"] != expected_id)
            or type(value.get("revision")) is not int or value["revision"] < 1
            or not all(isinstance(value.get(field), str) and value[field] for field in ("created_at", "updated_at"))
            or "current_scene" not in value):
        raise ProjectError("项目记录身份或版本无效；不会接管或覆盖。", "project_manifest_changed", 409)
    _name(value.get("name"))
    if value["current_scene"] is not None:
        _selection(value["current_scene"])
    value["workspace"] = _workspace_state(value.get("workspace"), value["current_scene"])
    return value


def _legacy_manifest(path, expected_id=None, *, pending_migration=False):
    value = _read(path)
    workspace = value.get("workspace") if isinstance(value, dict) else None
    if (not pending_migration and isinstance(workspace, dict)
            and any(field in workspace for field in (
                "rounds_since_judge", "rounds_since_judge_status", "judge_watermark"))):
        raise ProjectError("旧投影含已迁移状态但权威数据库缺失；保留现场待恢复。",
                           "project_state_missing", 409)
    return _manifest(value, expected_id)


class ProjectRepository:
    def __init__(self, root):
        root = Path(root)
        _plain(root)
        self.root = root.resolve()
        self.registry_path = self.root / "registry.json"
        self.lock = threading.RLock()
        self._handles = {}
        self._anchors = {}
        self._states = {}
        self._closed = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _plain(self.registry_path)
            if self.registry_path.exists():
                value = _read(self.registry_path)
                if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or not isinstance(value.get("projects"), list):
                    raise ProjectError("项目注册表无效；原文件已保留。", "project_storage_error", 500)
                self._entries = value["projects"]
                ids, paths = set(), set()
                for entry in self._entries:
                    if (not isinstance(entry, dict) or set(entry) != {"id", "path"}
                            or not isinstance(entry["id"], str) or not _ID.fullmatch(entry["id"])
                            or not isinstance(entry["path"], str) or not Path(entry["path"]).is_absolute()):
                        raise ProjectError("项目注册表条目无效；原文件已保留。", "project_storage_error", 500)
                    key = os.path.normcase(str(Path(entry["path"]).resolve()))
                    if entry["id"] in ids or key in paths:
                        raise ProjectError("项目注册表包含重复身份或目录。", "project_storage_error", 500)
                    ids.add(entry["id"])
                    paths.add(key)
            else:
                if any(self.root.iterdir()):
                    raise ProjectError("项目注册目录已有未识别内容；不会覆盖。", "project_storage_error", 500)
                self._entries = []
                _write(self.registry_path, {"schema_version": 1, "projects": []})
        except OSError:
            raise ProjectError("无法打开项目注册目录。", "project_storage_error", 500) from None

    def _assert_open(self):
        if self._closed:
            raise ProjectError("项目库已经关闭。", "project_storage_error", 500)

    def _entry(self, identifier):
        self._assert_open()
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ProjectError("项目不存在。", "project_not_found", 404)
        for entry in self._entries:
            if entry["id"] == identifier:
                return entry
        raise ProjectError("项目不存在。", "project_not_found", 404)

    def _directory(self, value):
        if isinstance(value, Path):
            value = str(value)
        if not isinstance(value, str) or not value or len(value) > 4096 or _CONTROL.search(value):
            raise ProjectError("请填写已有项目文件夹的绝对路径。", "invalid_project_path")
        path = Path(value)
        if (not path.is_absolute() or (os.name == "nt" and value.startswith(("\\\\", "//")))
                or any(part.lower() in {".git", ".sceneloop"} for part in path.parts)):
            raise ProjectError("请选择本地项目文件夹，不能使用相对路径或内部管理目录。", "invalid_project_path")
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ProjectError("项目文件夹不存在或不可访问。", "project_unavailable", 404) from None
        if not path.is_dir() or path == Path(path.anchor):
            raise ProjectError("请选择已有项目文件夹，不能选择文件或整个磁盘根目录。", "invalid_project_path")
        if any(part.lower() in {".git", ".sceneloop"} for part in path.parts):
            raise ProjectError("项目路径不能指向内部管理目录。", "invalid_project_path")
        return path

    def _acquire(self, directory, *, create=False):
        metadata = directory / ".sceneloop"
        _plain(metadata)
        if metadata.exists() and not metadata.is_dir():
            raise ProjectError(".sceneloop 已被其他文件占用，不能创建项目。", "project_storage_error", 409)
        manifest = metadata / "project.json"
        if (not create and not manifest.is_file()
                and not (metadata / "state.sqlite3").is_file()
                and not (metadata / "state-authority.json").is_file()):
            raise ProjectError("项目标记丢失；不会重新创建或接管该目录。", "project_manifest_changed", 409)
        if (create and metadata.exists() and not manifest.exists()
                and not (metadata / "state.sqlite3").is_file()
                and not (metadata / "state-authority.json").is_file()
                and any(p.name != ".server.lock" for p in metadata.iterdir())):
            raise ProjectError("文件夹已有未识别的 .sceneloop 内容；不会接管或覆盖。", "project_storage_error", 409)
        for path in (manifest, metadata / ".server.lock", metadata / "runs"):
            _plain(path)
        if (metadata / "runs").exists() and not (metadata / "runs").is_dir():
            raise ProjectError("项目任务目录被其他文件占用。", "project_storage_error", 409)
        key = os.path.normcase(str(directory))
        if key in self._handles:
            try:
                identity = tuple((info.st_dev, info.st_ino) for info in (metadata.stat(), (metadata / ".server.lock").stat()))
            except OSError:
                raise ProjectError("项目写锁目录已改变；已停止使用该工作区。", "project_manifest_changed", 409) from None
            if identity != self._anchors[key]:
                raise ProjectError("项目写锁或管理目录被替换；已停止使用该工作区。", "project_manifest_changed", 409)
        else:
            try:
                handle = DataDirectoryLock(metadata).acquire()
                try:
                    _plain(metadata)
                    _plain(metadata / ".server.lock")
                    self._anchors[key] = tuple((info.st_dev, info.st_ino) for info in (metadata.stat(), (metadata / ".server.lock").stat()))
                except Exception:
                    handle.close()
                    raise
                self._handles[key] = handle
            except DataDirectoryInUse:
                raise ProjectError("项目正在由另一个 SceneLoop 服务使用，请复用该服务。", "project_busy", 409) from None
            except OSError:
                raise ProjectError("无法锁定项目目录，请检查文件夹权限。", "project_storage_error", 500) from None
        return manifest

    def state_store(self, identifier):
        """Return the project-local authority, never promoting a stale JSON projection."""
        with self.lock:
            entry = self._entry(identifier)
            directory = self._directory(entry["path"])
            manifest_path = self._acquire(directory)
            store = self._states.get(identifier)
            if store is None:
                try:
                    store = ProjectStateStore(directory, identifier)
                    if store.ensure_authority() is None:
                        legacy = _legacy_manifest(
                            manifest_path, identifier,
                            pending_migration=store._authority is not None)
                        store.initialize(legacy, legacy_manifest_path=manifest_path)
                except StateStoreError as error:
                    raise ProjectError(str(error), error.code, error.status) from None
                self._states[identifier] = store
            return store

    @staticmethod
    def _project_to_manifest(project):
        return {key: copy.deepcopy(value) for key, value in project.items()
                if key not in {"path", "available", "scene_availability"}}

    @staticmethod
    def _project_projection(directory, manifest):
        # SQL is authoritative. A JSON compatibility projection is best effort.
        try:
            _write(directory / ".sceneloop" / "project.json", manifest)
        except ProjectError:
            pass

    def _public(self, value, directory):
        return {**copy.deepcopy(value), "path": str(directory), "available": True}

    def open(self, path, name=None):
        with self.lock:
            self._assert_open()
            directory = self._directory(path)
            if name is not None:
                _name(name)
            key = os.path.normcase(str(directory))
            for entry in self._entries:
                if os.path.normcase(str(Path(entry["path"]).resolve())) == key:
                    return self.get(entry["id"])
            already_held = key in self._handles
            try:
                manifest_path = self._acquire(directory, create=True)
                identity = ProjectStateStore.discover_identity(directory)
                created = identity is None and not manifest_path.exists()
                if identity is not None:
                    if any(entry["id"] == identity for entry in self._entries):
                        raise ProjectError("另一目录已登记相同项目身份，请明确迁移项目后再打开。",
                                           "project_identity_conflict", 409)
                    state = ProjectStateStore(directory, identity)
                    loaded = state.ensure_authority()
                    if loaded is None:
                        manifest = _manifest(_read(manifest_path), identity)
                        loaded = state.initialize(manifest, legacy_manifest_path=manifest_path)
                    manifest = _manifest(loaded[0], identity)
                else:
                    if not created:
                        manifest = _legacy_manifest(manifest_path)
                    else:
                        timestamp = _now()
                        manifest = {"schema_version": 1, "id": uuid.uuid4().hex,
                                    "name": _name(name, directory.name), "revision": 1,
                                    "current_scene": None, "workspace": _workspace_state(None),
                                    "created_at": timestamp, "updated_at": timestamp}
                        _write(manifest_path, manifest)
                    if any(entry["id"] == manifest["id"] for entry in self._entries):
                        raise ProjectError("另一目录已登记相同项目身份，请明确迁移项目后再打开。",
                                           "project_identity_conflict", 409)
                    state = ProjectStateStore(directory, manifest["id"])
                    loaded = state.ensure_authority()
                    if loaded is None:
                        loaded = state.initialize(
                            manifest, legacy_manifest_path=manifest_path if not created else None)
                    manifest = _manifest(loaded[0], manifest["id"])
                entries = self._entries + [{"id": manifest["id"], "path": str(directory)}]
                _write(self.registry_path, {"schema_version": 1, "projects": entries})
                self._entries = entries
                return self._public(manifest, directory)
            except Exception as error:
                if not already_held and key in self._handles:
                    self._handles.pop(key).close()
                    self._anchors.pop(key, None)
                if isinstance(error, StateStoreError):
                    raise ProjectError(str(error), error.code, error.status) from None
                raise

    def get(self, identifier, *, verify_artifacts=True):
        with self.lock:
            entry = self._entry(identifier)
            directory = self._directory(entry["path"])
            # A replaced root alias must not silently redirect an existing project.
            if os.path.normcase(str(directory)) != os.path.normcase(entry["path"]):
                raise ProjectError("项目目录指向已改变；不会跟随到另一工作区。", "project_manifest_changed", 409)
            manifest_path = self._acquire(directory)
            state = self.state_store(identifier)
            try:
                loaded = state.load()
                if loaded is None:
                    loaded = state.initialize(_manifest(_read(manifest_path), identifier),
                                              legacy_manifest_path=manifest_path)
                public = self._public(_manifest(loaded[0], identifier), directory)
                if verify_artifacts:
                    public["scene_availability"] = state.active_head_status()
                return public
            except StateStoreError as error:
                raise ProjectError(str(error), error.code, error.status) from None

    def list(self):
        with self.lock:
            self._assert_open()
            result = []
            for entry in self._entries:
                try:
                    result.append(self.get(entry["id"]))
                except ProjectError as error:
                    result.append({"id": entry["id"], "name": Path(entry["path"]).name, "path": entry["path"],
                                   "revision": None, "current_scene": None, "available": False,
                                   "error": {"code": error.code, "message": str(error)}})
            return result

    def runs_root(self, identifier):
        project = self.get(identifier, verify_artifacts=False)
        directory = Path(project["path"]) / ".sceneloop" / "runs"
        _plain(directory)
        return directory

    def commit_builder_scene(self, identifier, command, receipt, mutate_workspace):
        """Publish Builder head, batch and issue with the receipt in one transaction."""
        with self.lock:
            state = self.state_store(identifier)
            existing = state.lookup_receipt(command["operation_id"])
            if existing["status"] == "committed":
                verified, _ = state.commit_scene(command, {}, {}, expected_generation=0)
                return verified, self.get(identifier, verify_artifacts=False)
            project = self.get(identifier, verify_artifacts=False)
            identity = command["identity"]
            if (project["revision"] != identity["expected_project_revision"]
                    or project["workspace"]["working_version"] != identity["expected_working_version"]
                    or project["current_scene"] != identity["source_selection"]):
                raise ProjectError("旧候选不能覆盖较新的工作场景。", "project_revision_conflict", 409)
            selection = _selection(command["target_selection"])
            updated = self._project_to_manifest(project)
            workspace = _workspace_state(updated["workspace"], updated["current_scene"])
            timestamp = _now()
            if updated["current_scene"] and updated["current_scene"] != selection:
                recovery = {"selection": copy.deepcopy(updated["current_scene"]),
                            "working_version": workspace["working_version"],
                            "created_at": timestamp, "reason": command["reason"]}
                workspace["recoveries"] = (workspace["recoveries"] + [recovery])[-workspace["recovery_limit"]:]
            if updated["current_scene"] != selection:
                workspace["working_version"] += 1
                judgement = workspace.get("latest_judgement")
                if isinstance(judgement, dict) and judgement.get("working_version") != workspace["working_version"]:
                    judgement["current"] = False
                    judgement["superseded_by_working_version"] = workspace["working_version"]
                for issue in workspace.get("issues", []):
                    if issue.get("status") == "verified" and issue.get("working_version") != workspace["working_version"]:
                        issue["status"] = "verified_on_prior_version"
            workspace["working_updated_at"] = timestamp
            workspace["working_reason"] = command["reason"]
            batch_before = copy.deepcopy(workspace.get("batch_start"))
            replacement = mutate_workspace(copy.deepcopy(workspace))
            workspace = _workspace_state(workspace if replacement is None else replacement, selection)
            updated.update(current_scene=selection, workspace=workspace,
                           revision=project["revision"] + 1, updated_at=timestamp)
            receipt = copy.deepcopy(receipt)
            receipt.update(source_selection=copy.deepcopy(project["current_scene"]),
                           target_selection=copy.deepcopy(selection),
                           project_revision=updated["revision"],
                           working_version=workspace["working_version"],
                           batch_before=batch_before,
                           batch_after=copy.deepcopy(workspace.get("batch_start")),
                           committed_at=timestamp)
            loaded = state.load()
            if loaded is None:
                raise ProjectError("项目状态不可用。", "project_storage_error", 500)
            saved, _ = state.commit_scene(command, updated, receipt, expected_generation=loaded[1])
            self._project_projection(Path(project["path"]), updated)
            return saved, self._public(updated, Path(project["path"]))

    def set_current(self, identifier, selection, expected_revision, *, reason="manual",
                    preserve_previous=True, operation_id=None, identity=None,
                    kind="adoption", artifacts=None):
        selection = _selection(selection)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ProjectError("请选择当前项目版本后再采用场景。", "invalid_project_revision")
        with self.lock:
            project = self.get(identifier, verify_artifacts=False)
            if project["current_scene"] == selection and expected_revision <= project["revision"]:
                return project
            if expected_revision != project["revision"]:
                raise ProjectError("项目当前场景已改变，请刷新后重新选择；原版本保留。", "project_revision_conflict", 409)
            updated = {key: copy.deepcopy(value) for key, value in project.items() if key not in {"path", "available", "scene_availability"}}
            workspace = _workspace_state(updated.get("workspace"), updated.get("current_scene"))
            timestamp = _now()
            if preserve_previous and updated.get("current_scene") and updated["current_scene"] != selection:
                recovery = {"selection": copy.deepcopy(updated["current_scene"]),
                            "working_version": workspace["working_version"],
                            "created_at": timestamp, "reason": reason}
                workspace["recoveries"] = (workspace["recoveries"] + [recovery])[-workspace["recovery_limit"]:]
            if updated.get("current_scene") != selection:
                workspace["working_version"] += 1
                judgement = workspace.get("latest_judgement")
                if isinstance(judgement, dict) and judgement.get("working_version") != workspace["working_version"]:
                    judgement["current"] = False
                    judgement["superseded_by_working_version"] = workspace["working_version"]
                for issue in workspace.get("issues", []):
                    if issue.get("status") == "verified" and issue.get("working_version") != workspace["working_version"]:
                        issue["status"] = "verified_on_prior_version"
            workspace["working_updated_at"] = timestamp
            workspace["working_reason"] = reason
            updated.update(current_scene=selection, workspace=workspace,
                           revision=project["revision"] + 1, updated_at=timestamp)
            directory = Path(project["path"])
            state = self.state_store(identifier)
            loaded = state.load()
            if loaded is None:
                raise ProjectError("项目状态不可用。", "project_storage_error", 500)
            if identity is None:
                identity = {"project_id": identifier, "run_id": selection["run_id"], "kind": kind,
                            "source_selection": project["current_scene"],
                            "expected_project_revision": expected_revision,
                            "expected_working_version": project["workspace"]["working_version"],
                            "goal_identity": {"source": "explicit_selection"}}
            if operation_id is None:
                operation_id = uuid.uuid5(uuid.UUID(identifier), json.dumps(
                    {"identity": identity, "selection": selection, "reason": reason},
                    ensure_ascii=False, sort_keys=True)).hex
                state.prepare_operation({"operation_id": operation_id, "identity": identity})
            artifacts = artifacts or []
            command = {"operation_id": operation_id, "identity": identity,
                       "project_id": identifier, "run_id": selection["run_id"], "kind": kind,
                       "source_selection": identity["source_selection"],
                       "expected_project_revision": identity["expected_project_revision"],
                       "expected_working_version": identity["expected_working_version"],
                       "target_selection": selection, "legacy_revision_id": selection["revision_id"],
                       "artifacts": artifacts, "reason": reason, "operations": []}
            receipt = {"operation_id": operation_id, "project_id": identifier,
                       "run_id": selection["run_id"], "kind": kind,
                       "artifacts": artifacts,
                       "artifact_digest": hashlib.sha256(
                           json.dumps(artifacts, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                       "global_revision_id": state.global_revision_id(
                           identifier, selection["run_id"], selection["revision_id"]),
                       "source_selection": project["current_scene"],
                       "target_selection": selection, "committed_at": timestamp,
                       "project_revision": updated["revision"],
                       "working_version": workspace["working_version"]}
            state.commit_scene(command, updated, receipt, expected_generation=loaded[1])
            self._project_projection(directory, updated)
            return self._public(updated, directory)

    def update_workspace(self, identifier, expected_revision, mutate):
        """Atomically update workspace metadata together with the project revision."""
        with self.lock:
            project = self.get(identifier, verify_artifacts=False)
            if expected_revision != project["revision"]:
                raise ProjectError("项目状态已改变，请刷新后重试。", "project_revision_conflict", 409)
            updated = {key: copy.deepcopy(value) for key, value in project.items() if key not in {"path", "available", "scene_availability"}}
            workspace = _workspace_state(updated.get("workspace"), updated.get("current_scene"))
            replacement = mutate(copy.deepcopy(workspace))
            updated["workspace"] = _workspace_state(workspace if replacement is None else replacement,
                                                     updated.get("current_scene"))
            # Project revision is the compare-and-swap generation for the working
            # scene pointer. Metadata-only conversation updates do not invalidate
            # an otherwise current edit request.
            updated["revision"] = project["revision"]
            updated["updated_at"] = _now()
            directory = Path(project["path"])
            state = self.state_store(identifier)
            loaded = state.load()
            if loaded is None:
                raise ProjectError("项目状态不可用。", "project_storage_error", 500)
            state.save_project(updated, expected_generation=loaded[1])
            self._project_projection(directory, updated)
            return self._public(updated, directory)

    def update_workspace_checked(self, identifier, check, mutate, *, accepted_judge=None,
                                  evaluation_receipt=None):
        """Apply mutate only when check accepts the project read under the same lock.

        Metadata updates do not bump the project revision, so a predicate over the
        project identity (not only the revision number) is the acceptance gate.
        The check must be side-effect free; both callables run under the repository
        lock and are not called again after the write.

        When ``evaluation_receipt`` is provided, the idempotent receipt check and
        the workspace/accepted_judges/receipt write share one SQL transaction.
        """
        with self.lock:
            project = self.get(identifier, verify_artifacts=False)
            if not check(project):
                return None
            updated = {key: copy.deepcopy(value) for key, value in project.items() if key not in {"path", "available", "scene_availability"}}
            workspace = _workspace_state(updated.get("workspace"), updated.get("current_scene"))
            replacement = mutate(copy.deepcopy(workspace))
            updated["workspace"] = _workspace_state(workspace if replacement is None else replacement,
                                                     updated.get("current_scene"))
            updated["revision"] = project["revision"]
            updated["updated_at"] = _now()
            directory = Path(project["path"])
            state = self.state_store(identifier)
            loaded = state.load()
            if loaded is None:
                raise ProjectError("项目状态不可用。", "project_storage_error", 500)
            _generation, receipt = state.save_project(
                updated, expected_generation=loaded[1],
                accepted_judge=accepted_judge, evaluation_receipt=evaluation_receipt)
            updated = state.load()[0]
            self._project_projection(directory, updated)
            result = self._public(updated, directory)
            if evaluation_receipt is not None:
                result["evaluation_receipt"] = receipt
            return result

    def close(self):
        """Only call after all project workers have joined; production keeps locks."""
        with self.lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()
            self._anchors.clear()
            self._states.clear()
            self._closed = True
