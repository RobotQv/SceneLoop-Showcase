"""One local SQLite authority for project scene metadata and commit receipts.

The project OS lock is owned by ProjectRepository. This store uses short SQLite
write transactions; Blender, providers, artifact hashing and backup copies run
before those transactions. A committed receipt, not a JSON projection, decides
whether an operation changed the working scene.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from urllib.parse import quote
import uuid


class StateStoreError(Exception):
    def __init__(self, message, code="state_storage_error", status=500):
        super().__init__(message)
        self.code, self.status = code, status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _not_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise StateStoreError("项目状态路径不能是符号链接或目录联接。", "unsafe_project_path", 409)


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        _not_link(path)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _authority_marker(path, expected_project_id=None):
    _not_link(path)
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise StateStoreError("场景状态权威标记无法读取。", "project_state_corrupt", 409) from None
    if (not isinstance(marker, dict) or marker.get("schema_version") != 1
            or marker.get("state") not in {"preparing", "upgrading", "complete"}
            or not isinstance(marker.get("project_id"), str)
            or not isinstance(marker.get("authority_id"), str)
            or len(marker["authority_id"]) != 32
            or any(char not in "0123456789abcdef" for char in marker["authority_id"])):
        raise StateStoreError("场景状态权威标记无效。", "project_state_corrupt", 409)
    if expected_project_id is not None and marker["project_id"] != expected_project_id:
        raise StateStoreError("场景状态权威身份不符。", "project_identity_conflict", 409)
    return marker


class ProjectStateStore:
    """Project-local state with one committed manifest and append-only receipts."""

    def _sql_has_authority(self):
        """One bootstrap judgment: SQL has taken over and JSON must never re-import."""
        return self._authority is not None and self._authority["state"] in {"complete", "upgrading"}

    @staticmethod
    def _ro_uri(database):
        return f"file:{quote(Path(database).as_posix(), safe='/:')}?mode=ro"

    def _probe_sql_state(self):
        """Read-only probe before schema creation; never modifies the file."""
        if not self.path.is_file():
            return "missing"
        try:
            with closing(sqlite3.connect(self._ro_uri(self.path), uri=True,
                                         timeout=5)) as connection:
                try:
                    row = connection.execute(
                        "SELECT 1 FROM project_state WHERE singleton=1").fetchone()
                except sqlite3.OperationalError as error:
                    if "no such table" not in str(error):
                        raise
                    return "empty"
                return "present" if row else "empty"
        except sqlite3.DatabaseError:
            return "corrupt"

    def __init__(self, project_root, project_id):
        self.project_root = Path(project_root).resolve()
        self.metadata = self.project_root / ".sceneloop"
        self.path = self.metadata / "state.sqlite3"
        self.authority_path = self.metadata / "state-authority.json"
        self.project_id = project_id
        _not_link(self.metadata)
        _not_link(self.path)
        if not self.metadata.is_dir():
            raise StateStoreError("项目管理目录不存在。")
        self._authority = _authority_marker(self.authority_path, project_id)
        self._bootstrap_guard()
        self._database_anchor = None
        self._legacy_backup_loaded = False
        self._legacy_backup_manifest = None
        self._legacy_projection_cache = {}
        self._schema()
        info = self.path.stat()
        self._database_anchor = (info.st_dev, info.st_ino)

    def _bootstrap_guard(self):
        """Unified first-migration vs existing-authority check before any write."""
        if self._sql_has_authority():
            sql_state = self._probe_sql_state()
            if sql_state == "missing":
                raise StateStoreError("权威场景状态数据库缺失；保留现有档案，需人工恢复。",
                                      "project_state_missing", 409)
            if sql_state != "present":
                raise StateStoreError("权威场景状态数据库缺失或为空；保留现有档案，需人工恢复。",
                                      "project_state_corrupt", 409)
        elif self._authority is None and not self.path.is_file():
            if (self.metadata / "state-migration-backup" / "manifest.json").exists():
                raise StateStoreError("旧迁移痕迹存在但权威数据库缺失；不能由投影自动重建。",
                                      "project_state_missing", 409)

    @staticmethod
    def discover_identity(project_root):
        """Read existing SQL identity before considering a JSON projection."""
        metadata = Path(project_root) / ".sceneloop"
        marker = _authority_marker(metadata / "state-authority.json")
        database = metadata / "state.sqlite3"
        _not_link(database)
        database_id = None
        if database.is_file():
            try:
                with closing(sqlite3.connect(ProjectStateStore._ro_uri(database),
                                             uri=True, timeout=5)) as connection:
                    try:
                        row = connection.execute(
                            "SELECT project_id FROM project_state WHERE singleton=1").fetchone()
                    except sqlite3.OperationalError as error:
                        if "no such table" not in str(error):
                            raise
                        row = None
                    if row:
                        database_id = row[0]
            except sqlite3.DatabaseError:
                raise StateStoreError("权威场景状态数据库损坏，不能从JSON投影重建。",
                                      "project_state_corrupt", 409) from None
        if marker and database_id and marker["project_id"] != database_id:
            raise StateStoreError("权威标记与数据库身份冲突。", "project_identity_conflict", 409)
        return database_id or (marker["project_id"] if marker else None)

    def _publish_authority(self, state, authority_id):
        marker = {"schema_version": 1, "project_id": self.project_id,
                  "authority_id": authority_id, "state": state}
        if marker == self._authority:
            return
        try:
            _write_json(self.authority_path, marker)
        except OSError:
            raise StateStoreError("无法持久记录场景状态权威身份。", "project_storage_error") from None
        self._authority = marker

    def ensure_authority(self):
        """Finish publication or upgrade a pre-marker database, preserving its row."""
        loaded = self.load()
        if loaded is None:
            if self._sql_has_authority():
                raise StateStoreError("已发布的权威数据库没有项目状态。",
                                      "project_state_corrupt", 409)
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT project_id,authority_id FROM authority_identity WHERE singleton=1").fetchone()
        if row is None and self._authority and self._authority["state"] == "complete":
            raise StateStoreError("权威数据库身份记录缺失。", "project_state_corrupt", 409)
        authority_id = (self._authority["authority_id"] if self._authority
                        else row["authority_id"] if row else uuid.uuid4().hex)
        if self._authority is None:
            # Existing pre-marker SQL may already contain receipts. Publish an
            # upgrade claim before any identity write, so its loss cannot look
            # like an untouched legacy JSON project.
            self._publish_authority("upgrading", authority_id)
        if row is None:
            with self._write() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO authority_identity VALUES (1,?,?)",
                    (self.project_id, authority_id))
                row = connection.execute(
                    "SELECT project_id,authority_id FROM authority_identity WHERE singleton=1").fetchone()
        if row["project_id"] != self.project_id:
            raise StateStoreError("权威数据库项目身份冲突。", "project_identity_conflict", 409)
        if self._authority and row["authority_id"] != self._authority["authority_id"]:
            raise StateStoreError("权威数据库身份已变化。", "project_identity_conflict", 409)
        self._publish_authority("complete", row["authority_id"])
        return loaded

    @contextmanager
    def _connection(self):
        _not_link(self.path)
        if self._database_anchor is not None:
            try:
                info = self.path.stat()
            except OSError:
                raise StateStoreError("项目状态数据库已消失。", "project_manifest_changed", 409) from None
            if (info.st_dev, info.st_ino) != self._database_anchor:
                raise StateStoreError("项目状态数据库身份已变化。", "project_manifest_changed", 409)
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        except sqlite3.OperationalError as error:
            raise StateStoreError("项目状态数据库忙或无法访问。", "state_busy", 503) from error
        except sqlite3.DatabaseError as error:
            raise StateStoreError("项目状态数据库损坏或写入失败。", "state_storage_error") from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _write(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _schema(self):
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS authority_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    project_id TEXT NOT NULL,
                    authority_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    project_id TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refinement_goals (
                    goal_id TEXT PRIMARY KEY, version INTEGER NOT NULL UNIQUE, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refinement_bindings (
                    run_id TEXT PRIMARY KEY, binding TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refinement_plans (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refinement_plan_versions (
                    version INTEGER PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refinement_updates (
                    update_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_digest TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_rounds (
                    round_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS local_rounds_run ON local_rounds(run_id,created_at);
                CREATE TABLE IF NOT EXISTS local_round_resumes (
                    resume_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_round_dispatches (
                    resume_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('accepted','started')),
                    started_at TEXT
                );
                CREATE TABLE IF NOT EXISTS asset_run_capabilities (
                    run_id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_run_requests (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                    slot INTEGER NOT NULL CHECK(slot=1), version INTEGER NOT NULL,
                    job_id TEXT UNIQUE, grant_id TEXT UNIQUE,
                    closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0,1)),
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_run_resumes (
                    request_id TEXT PRIMARY KEY, resume_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('accepted','started'))
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    run_id TEXT,
                    kind TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('prepared', 'unknown', 'committed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    edit_retry_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS run_counters (
                    run_id TEXT PRIMARY KEY,
                    saved_edit_rounds INTEGER,
                    needs_reconciliation INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS edit_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    retry_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    failure_category TEXT,
                    retryable INTEGER,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
                );
                CREATE TABLE IF NOT EXISTS accepted_judges (
                    judgement_id TEXT PRIMARY KEY,
                    selection TEXT NOT NULL,
                    working_version INTEGER NOT NULL,
                    accepted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scene_revisions (
                    revision_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    legacy_revision_id TEXT,
                    selection TEXT NOT NULL,
                    artifacts TEXT NOT NULL,
                    availability TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    run_id TEXT,
                    kind TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
                );
                CREATE TABLE IF NOT EXISTS reconciliation (
                    key TEXT PRIMARY KEY,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_inputs (
                    input_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('open', 'sealed', 'abandoned')),
                    seal_state TEXT,
                    sealed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_receipts (
                    evaluation_id TEXT PRIMARY KEY,
                    input_id TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    verdict TEXT,
                    scope TEXT,
                    application_status TEXT NOT NULL,
                    application_reason TEXT,
                    receipt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_generation_jobs (
                    project_id TEXT NOT NULL,
                    job_id TEXT PRIMARY KEY,
                    submission_key TEXT NOT NULL,
                    generation_authorization_id TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    UNIQUE(project_id, submission_key),
                    UNIQUE(project_id, generation_authorization_id)
                );
                CREATE TABLE IF NOT EXISTS asset_generation_events (
                    job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    grant_scope TEXT,
                    grant_id TEXT,
                    document TEXT NOT NULL,
                    PRIMARY KEY(job_id, sequence),
                    UNIQUE(project_id, grant_scope, grant_id),
                    FOREIGN KEY(job_id) REFERENCES asset_generation_jobs(job_id)
                );
            """)

    @staticmethod
    def _read_row(connection):
        return connection.execute(
            "SELECT project_id, manifest, generation FROM project_state WHERE singleton=1").fetchone()

    def load(self):
        with self._connection() as connection:
            row = self._read_row(connection)
            if row is None:
                if self._sql_has_authority():
                    raise StateStoreError("已发布的权威数据库没有项目状态。",
                                          "project_state_corrupt", 409)
                return None
            if row["project_id"] != self.project_id:
                raise StateStoreError("状态数据库的项目身份与注册表不符。", "project_identity_conflict", 409)
            return json.loads(row["manifest"]), row["generation"]

    def _legacy_backup(self, manifest_path):
        """Preserve original metadata once; no blend, images, keys or credentials."""
        backup = self.metadata / "state-migration-backup"
        _not_link(backup)
        source = Path(manifest_path)
        _not_link(source)
        if not source.is_file():
            raise StateStoreError("旧项目标记丢失，无法迁移。", "project_manifest_changed", 409)
        source_hash = _file_hash(source)
        marker = backup / "manifest.json"
        if marker.exists():
            _not_link(marker)
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if recorded.get("project_sha256") != source_hash:
                raise StateStoreError("迁移备份与原项目标记不符，停止重试。", "project_manifest_changed", 409)
            saved_project = backup / "project.json"
            _not_link(saved_project)
            if not saved_project.is_file() or _file_hash(saved_project) != source_hash:
                raise StateStoreError("原项目备份文件缺失或变化，停止重试。", "project_manifest_changed", 409)
            for row in recorded.get("run_files", []):
                saved = backup / row["backup"]
                _not_link(saved)
                if not saved.is_file() or _file_hash(saved) != row["sha256"]:
                    raise StateStoreError("迁移备份文件缺失或变化，停止重试。", "project_manifest_changed", 409)
            return recorded
        backup.mkdir(exist_ok=True)
        _not_link(backup)
        copied = backup / "project.json"
        _not_link(copied)
        shutil.copyfile(source, copied)
        if _file_hash(copied) != source_hash:
            raise StateStoreError("项目备份校验失败，原文件保留。")
        run_files = []
        runs_root = self.metadata / "runs"
        if runs_root.is_dir():
            _not_link(runs_root)
            for path in sorted(runs_root.glob("*/run.json")):
                _not_link(path.parent)
                _not_link(path)
                if not path.is_file() or path.parent.parent != runs_root:
                    continue
                run_id = path.parent.name
                if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
                    continue
                relative = Path("runs") / run_id / "run.json"
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _not_link(target.parent)
                _not_link(target)
                shutil.copyfile(path, target)
                hashed = _file_hash(path)
                if _file_hash(target) != hashed:
                    raise StateStoreError("任务记录备份校验失败，原文件保留。")
                run_files.append({"source": relative.as_posix(), "backup": relative.as_posix(),
                                  "sha256": hashed})
        recorded = {"schema_version": 1, "project_id": self.project_id,
                    "project_sha256": source_hash, "run_files": run_files,
                    "created_at": _now()}
        _write_json(marker, recorded)
        return recorded

    @staticmethod
    def global_revision_id(project_id, run_id, legacy_revision_id):
        return uuid.uuid5(uuid.UUID(project_id), f"{run_id}:{legacy_revision_id}").hex

    def _legacy_rounds_since_judge(self, manifest, backup):
        """Infer only a complete, self-consistent legacy batch watermark."""
        current = manifest.get("current_scene")
        if current is None:
            return 0
        workspace = manifest.get("workspace") or {}
        batch = workspace.get("batch_start")
        if not isinstance(batch, dict) or batch.get("status") not in {"active", "passed"}:
            return None
        edits, judged = batch.get("edit_rounds"), batch.get("last_judged_round")
        start, working = batch.get("working_version"), workspace.get("working_version")
        if (type(edits) is not int or edits < 1 or type(judged) is not int
                or judged < 0 or judged > edits or type(start) is not int
                or type(working) is not int or working - start != edits
                or (batch["status"] == "passed" and judged != edits)):
            return None
        owner = next((row for row in backup["run_files"]
                      if row["source"] == f"runs/{current['run_id']}/run.json"), None)
        if owner is None:
            return None
        try:
            run = json.loads((self.metadata / "state-migration-backup" / owner["backup"])
                             .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(run, dict) or run.get("project_id") != self.project_id
                or run.get("operation") != "edit"):
            return None
        commits = run.get("commits")
        if not isinstance(commits, list) or not any(
                isinstance(row, dict) and row.get("revision_id") == current["revision_id"]
                and row.get("working_version") == working
                and row.get("project_revision") == manifest.get("revision")
                for row in commits):
            return None
        return edits - judged

    def initialize(self, manifest, *, legacy_manifest_path=None):
        """Publish an initial state only after a complete optional legacy backup."""
        if manifest.get("id") != self.project_id:
            raise StateStoreError("项目身份不匹配。", "project_identity_conflict", 409)
        if self._sql_has_authority():
            return self.ensure_authority()
        already = self.load()
        if already is not None:
            return self.ensure_authority()
        authority_id = (self._authority["authority_id"] if self._authority
                        else uuid.uuid4().hex)
        self._publish_authority("preparing", authority_id)
        backup = self._legacy_backup(legacy_manifest_path) if legacy_manifest_path else None
        manifest = copy.deepcopy(manifest)
        workspace = manifest.setdefault("workspace", {})
        inferred = (self._legacy_rounds_since_judge(manifest, backup) if backup
                    else 0 if manifest.get("current_scene") is None else None)
        workspace.setdefault("rounds_since_judge", inferred)
        workspace.setdefault("rounds_since_judge_status",
                             "known" if inferred is not None else "needs_reconciliation")
        workspace.setdefault("judge_watermark", None)
        with self._write() as connection:
            row = self._read_row(connection)
            if row is not None:
                if row["project_id"] != self.project_id:
                    raise StateStoreError("状态数据库身份不匹配。", "project_identity_conflict", 409)
                return json.loads(row["manifest"]), row["generation"]
            connection.execute(
                "INSERT INTO authority_identity(singleton,project_id,authority_id) VALUES (1,?,?)",
                (self.project_id, authority_id))
            connection.execute(
                "INSERT INTO project_state(singleton,project_id,manifest,generation) VALUES (1,?,?,1)",
                (self.project_id, _encoded(manifest)))
            if backup:
                connection.execute(
                    "INSERT INTO reconciliation(key,detail) VALUES ('migration_backup',?)",
                    (_encoded(backup),))
                self._import_legacy_mappings(connection, manifest, backup)
                self._import_legacy_counters(connection, manifest, backup)
        self._publish_authority("complete", authority_id)
        return copy.deepcopy(manifest), 1

    def _import_legacy_mappings(self, connection, manifest, backup):
        rows = {}
        for item in backup["run_files"]:
            path = self.metadata / "state-migration-backup" / item["backup"]
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(run, dict) or run.get("project_id") != self.project_id:
                continue
            run_id = run.get("id")
            revisions = []
            agent = run.get("agent")
            if isinstance(agent, dict) and isinstance(agent.get("revisions"), list):
                revisions = [row.get("id") for row in agent["revisions"]
                             if isinstance(row, dict) and isinstance(row.get("id"), str)]
            artifacts = run.get("artifacts")
            if not revisions and isinstance(artifacts, list):
                revisions = [None] if any(
                    a.get("kind") == "blend" for a in artifacts if isinstance(a, dict)) else []
            for legacy_id in revisions:
                key = (run_id, legacy_id)
                rows[key] = {"run_id": run_id, "revision_id": legacy_id}
        current = manifest.get("current_scene")
        if isinstance(current, dict):
            key = (current.get("run_id"), current.get("revision_id"))
            rows.setdefault(key, {"run_id": key[0], "revision_id": key[1]})
        for (run_id, legacy_id), value in rows.items():
            if not isinstance(run_id, str):
                continue
            revision_id = self.global_revision_id(self.project_id, run_id, legacy_id)
            selection = (current if isinstance(current, dict) and current.get("run_id") == run_id
                         and current.get("revision_id") == legacy_id else value)
            connection.execute(
                "INSERT OR IGNORE INTO scene_revisions VALUES (?,?,?,?,?,?,?)",
                (revision_id, run_id, legacy_id, _encoded(selection), "[]", "unknown", _now()))
        if current:
            owner = next((row for row in backup["run_files"]
                          if row["source"] == f"runs/{current['run_id']}/run.json"), None)
            if owner:
                saved = json.loads((self.metadata / "state-migration-backup" / owner["backup"]).read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    saved = {}
                old_commits = saved.get("commits")
                committed = any(row.get("revision_id") == current.get("revision_id")
                                for row in old_commits if isinstance(row, dict)) if isinstance(old_commits, list) else False
                initial = bool(saved.get("initial_working_commit"))
                if not committed and not initial:
                    connection.execute(
                        "INSERT INTO reconciliation(key,detail) VALUES ('legacy_head_without_run_commit',?)",
                        (_encoded({"selection": current, "note": "unknown legacy outcome; no receipt synthesized"}),))
            else:
                connection.execute(
                    "INSERT INTO reconciliation(key,detail) VALUES ('legacy_current_run_missing',?)",
                    (_encoded({"selection": current, "note": "active legacy run metadata unavailable"}),))

    def _import_legacy_counters(self, connection, manifest, backup):
        current = manifest.get("current_scene") or {}
        for item in backup["run_files"]:
            saved = self.metadata / "state-migration-backup" / item["backup"]
            try:
                run = json.loads(saved.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(run, dict):
                continue
            run_id = run.get("id")
            if not isinstance(run_id, str) or run.get("project_id") != self.project_id:
                continue
            commits = run.get("commits")
            valid = isinstance(commits, list) and all(
                isinstance(row, dict) and isinstance(row.get("revision_id"), str)
                and type(row.get("working_version")) is int
                and type(row.get("project_revision")) is int
                for row in commits)
            if valid:
                unique = {(row["revision_id"], row.get("working_version")) for row in commits}
                valid = len(unique) == len(commits)
                if run.get("operation") == "edit" and not commits:
                    valid = False
            if (current.get("run_id") == run_id and
                    not any(row.get("revision_id") == current.get("revision_id")
                            for row in commits or [] if isinstance(row, dict)) and
                    not run.get("initial_working_commit")):
                valid = False
            value = len(commits) if valid else None
            connection.execute(
                "INSERT OR IGNORE INTO run_counters(run_id,saved_edit_rounds,needs_reconciliation)"
                " VALUES (?,?,?)", (run_id, value, 0 if valid else 1))

    def _check_new_evaluation_receipt(self, connection, receipt):
        """Check the sealed host input and raw result before a new receipt write."""
        from .evaluation_input import EvaluationInputError, result_digest, validate_manifest
        row = connection.execute(
            "SELECT manifest,input_digest,state,seal_state FROM evaluation_inputs WHERE input_id=?",
            (receipt.get("input_id"),)).fetchone()
        if row is None:
            raise StateStoreError("评审输入未登记。", "evaluation_input_missing", 409)
        if row["state"] != "sealed":
            raise StateStoreError("评审输入未封存。", "evaluation_input_not_sealed", 409)
        source = json.loads(row["manifest"])
        require_sent = receipt.get("application_status") in {"applied", "candidate_registered"}
        try:
            validate_manifest(source, require_sent=require_sent)
        except EvaluationInputError as error:
            raise StateStoreError(str(error), error.code, 409) from None
        if (receipt.get("project_id") != self.project_id
                or receipt.get("evaluation_id") != source["evaluation_id"]
                or receipt.get("run_id") != source["run_id"]
                or receipt.get("input_digest") != row["input_digest"]
                or source["project_id"] != self.project_id):
            raise StateStoreError("评审输入身份不符。", "evaluation_input_mismatch", 409)
        raw = receipt.get("result")
        projection = receipt.get("projection")
        if (not isinstance(raw, dict) or result_digest(raw) != receipt.get("result_digest")
                or not isinstance(projection, dict)
                or projection.get("input_id") != source["input_id"]
                or not isinstance(projection.get("judgement"), dict)):
            raise StateStoreError("评审结果或投影未与输入绑定。", "evaluation_input_mismatch", 409)
        if (raw.get("evaluation_id", source["evaluation_id"]) != source["evaluation_id"]
                or raw.get("input_id", source["input_id"]) != source["input_id"]
                or raw.get("input_digest", source["input_digest"]) != source["input_digest"]):
            raise StateStoreError("评审结果身份不符。", "evaluation_input_mismatch", 409)
        context = source.get("context") or {}
        if (raw.get("target") is not None and raw["target"] != context.get("target")
                or receipt.get("scope") != context.get("scope")):
            raise StateStoreError("评审目标或范围不符。", "evaluation_input_mismatch", 409)
        return source

    def save_project(self, manifest, *, expected_generation, accepted_judge=None,
                     evaluation_receipt=None):
        with self._write() as connection:
            receipt_result = None
            if evaluation_receipt is not None:
                existing = connection.execute(
                    "SELECT receipt,input_digest,result_digest FROM evaluation_receipts"
                    " WHERE evaluation_id=?", (evaluation_receipt["evaluation_id"],)).fetchone()
                if existing:
                    if (existing["input_digest"] != evaluation_receipt["input_digest"]
                            or existing["result_digest"] != evaluation_receipt["result_digest"]):
                        raise StateStoreError("相同评审 ID 对应不同输入或结果。",
                                              "evaluation_conflict", 409)
                    return expected_generation, json.loads(existing["receipt"])
                self._check_new_evaluation_receipt(connection, evaluation_receipt)
            row = self._read_row(connection)
            if row is None or row["project_id"] != self.project_id:
                raise StateStoreError("项目状态尚未完成迁移。", "state_unavailable", 409)
            if row["generation"] != expected_generation:
                raise StateStoreError("项目元数据已变化，请刷新重试。", "project_revision_conflict", 409)
            manifest = copy.deepcopy(manifest)
            if accepted_judge is not None:
                selection = accepted_judge["selection"]
                working_version = accepted_judge["working_version"]
                if (manifest.get("current_scene") != selection or
                        manifest["workspace"].get("working_version") != working_version):
                    raise StateStoreError("Judge 目标已不是当前工作版。", "judge_stale", 409)
                prior = connection.execute(
                    "SELECT judgement_id FROM accepted_judges WHERE judgement_id=?",
                    (accepted_judge["id"],)).fetchone()
                if prior is None:
                    timestamp = _now()
                    connection.execute(
                        "INSERT INTO accepted_judges VALUES (?,?,?,?)",
                        (accepted_judge["id"], _encoded(selection), working_version, timestamp))
                    manifest["workspace"]["rounds_since_judge"] = 0
                    manifest["workspace"]["rounds_since_judge_status"] = "known"
                    manifest["workspace"]["judge_watermark"] = {
                        "judgement_id": accepted_judge["id"],
                        "selection": copy.deepcopy(selection),
                        "working_version": working_version, "accepted_at": timestamp}
            if evaluation_receipt is not None:
                timestamp = _now()
                receipt_data = {**copy.deepcopy(evaluation_receipt), "created_at": timestamp}
                connection.execute(
                    "INSERT INTO evaluation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (evaluation_receipt["evaluation_id"], evaluation_receipt["input_id"],
                     evaluation_receipt["input_digest"], evaluation_receipt["result_digest"],
                     evaluation_receipt["run_id"], evaluation_receipt["project_id"],
                     evaluation_receipt.get("verdict"),
                     _encoded(evaluation_receipt.get("scope")),
                     evaluation_receipt["application_status"],
                     evaluation_receipt.get("application_reason"),
                     _encoded(receipt_data), timestamp))
                receipt_result = receipt_data
            generation = expected_generation + 1
            connection.execute("UPDATE project_state SET manifest=?,generation=? WHERE singleton=1",
                               (_encoded(manifest), generation))
            return generation, receipt_result

    def refinement_state(self, run_id=None):
        """Read-only SQL snapshot; opening/reading never creates a work plan."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            plan = connection.execute("SELECT document FROM refinement_plans WHERE singleton=1").fetchone()
            goal = connection.execute("SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1").fetchone()
            binding = (connection.execute("SELECT binding FROM refinement_bindings WHERE run_id=?",
                                          (run_id,)).fetchone() if run_id else None)
            return {"plan": json.loads(plan[0]) if plan else None,
                    "goal": json.loads(goal[0]) if goal else None,
                    "binding": json.loads(binding[0]) if binding else None}

    def bind_refinement(self, run):
        """Bind one executing request and evolve only goal/Plan metadata."""
        from .refinement_plan import goal_content, selection_identity
        with self._write() as connection:
            existing = connection.execute("SELECT binding FROM refinement_bindings WHERE run_id=?",
                                          (run["id"],)).fetchone()
            if existing:
                return json.loads(existing[0])
            project = json.loads(self._read_row(connection)["manifest"])
            if run.get("operation") == "edit":
                expected = selection_identity(run.get("working_scene") or run.get("base_scene"))
                if (project["revision"] != run["expected_project_revision"]
                        or selection_identity(project.get("current_scene")) != expected):
                    raise StateStoreError("执行前场景绑定已失效。", "project_changed", 409)
            row = connection.execute("SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1").fetchone()
            previous = json.loads(row[0]) if row else None
            content, discussion = goal_content(run, project, previous)
            content_digest = _digest(content)
            goal = previous
            if previous is None or previous["content_digest"] != content_digest:
                goal = {"id": uuid.uuid4().hex, "version": previous["version"] + 1 if previous else 1,
                        "project_id": self.project_id, "content": content, "content_digest": content_digest,
                        "source": {"kind": "legacy_workspace" if discussion and previous is None else "execution_request",
                                   "run_id": run["id"], "legacy": copy.deepcopy(project["workspace"].get("goal"))
                                   if previous is None else None}, "created_at": _now()}
                connection.execute("INSERT INTO refinement_goals VALUES (?,?,?)",
                                   (goal["id"], goal["version"], _encoded(goal)))
            row = connection.execute("SELECT document FROM refinement_plans WHERE singleton=1").fetchone()
            plan = json.loads(row[0]) if row else None
            changed = plan is None or plan["goal_id"] != goal["id"]
            if plan is None:
                plan = {"schema_version": 1, "id": uuid.uuid4().hex, "project_id": self.project_id,
                        "version": 1, "goal_id": goal["id"], "focus_item_id": None,
                        "order": [], "items": [], "created_at": _now()}
            elif changed:
                plan.update(goal_id=goal["id"], version=plan["version"] + 1)
            if changed:
                connection.execute("INSERT OR REPLACE INTO refinement_plans VALUES (1,?)", (_encoded(plan),))
                connection.execute("INSERT INTO refinement_plan_versions VALUES (?,?)", (plan["version"], _encoded(plan)))
            binding = {"run_id": run["id"], "project_id": self.project_id, "goal": goal,
                       "plan_id": plan["id"], "bound_plan_version": plan["version"],
                       "scene": selection_identity(project.get("current_scene")),
                       "working_version": project["workspace"]["working_version"],
                       "request": run["prompt"], "discussion": discussion}
            connection.execute("INSERT INTO refinement_bindings VALUES (?,?)", (run["id"], _encoded(binding)))
            return binding

    def bind_director_parent(self, run, *, director_store=None):
        """A continued user request references the existing task Goal claim."""
        from .refinement_plan import selection_identity
        with self._write() as connection:
            existing=connection.execute('SELECT binding FROM refinement_bindings WHERE run_id=?',(run['id'],)).fetchone()
            if existing: return json.loads(existing[0])
            task=json.loads(connection.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
            goal=json.loads(connection.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()[0])
            plan=json.loads(connection.execute('SELECT document FROM refinement_plans WHERE singleton=1').fetchone()[0])
            project=json.loads(self._read_row(connection)['manifest'])
            from .director_goal_continuity import continue_goal
            continue_goal(director_store,connection,run,task,goal,plan,project)
            binding={'run_id':run['id'],'project_id':self.project_id,'goal':goal,'plan_id':plan['id'],
                'bound_plan_version':plan['version'],'scene':selection_identity(project.get('current_scene')),
                'working_version':project['workspace']['working_version'],'request':run['prompt'],'discussion':False}
            connection.execute('INSERT INTO refinement_bindings VALUES (?,?)',(run['id'],_encoded(binding)))
        return binding

    def bind_director_child(self, run, delegated):
        """A SQL-trusted child references the parent's Goal, never its local prompt."""
        from .refinement_plan import selection_identity
        with self._write() as connection:
            row = connection.execute('SELECT document FROM director_delegations WHERE child_id=?',(run['id'],)).fetchone()
            trusted = json.loads(row[0]) if row else None
            if not delegated or trusted != delegated or trusted['state'] != 'started':
                raise StateStoreError('未找到已启动的可信委派。','director_unbound',409)
            existing = connection.execute('SELECT binding FROM refinement_bindings WHERE run_id=?',(run['id'],)).fetchone()
            if existing: return json.loads(existing[0])
            parent = connection.execute('SELECT binding FROM refinement_bindings WHERE run_id=?',(delegated['parent_id'],)).fetchone()
            latest = connection.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()
            project = json.loads(self._read_row(connection)['manifest'])
            binding = json.loads(parent[0]) if parent else None
            goal = json.loads(latest[0]) if latest else None
            if (not binding or not goal or binding['goal']['id'] != goal['id']
                or {k:goal[k] for k in ('id','version','content_digest')} != delegated['brief']['goal_ref']
                or selection_identity(project.get('current_scene')) != selection_identity(delegated['brief']['baseline']['selection'])
                or project['workspace']['working_version'] != delegated['brief']['baseline']['working_version']
                or project['revision'] != run['expected_project_revision']):
                raise StateStoreError('冻结委派基线或全局目标已变化。','director_stale',409)
            authorization=delegated['brief'].get('plan_authorization')
            if authorization:
                plan=json.loads(connection.execute('SELECT document FROM refinement_plans WHERE singleton=1').fetchone()[0])
                from .director_goal_plan import plan_authorization
                if plan_authorization(plan,list(authorization['items']))!=authorization:
                    raise StateStoreError('冻结委派Plan版本/验收效果已变化。','director_stale',409)
            binding = {**binding,'run_id':run['id'],'request':run['prompt'],'discussion':False,
                       'director_delegation_id':delegated['delegation_id'],
                       'scene':selection_identity(project.get('current_scene')),
                       'working_version':project['workspace']['working_version']}
            connection.execute('INSERT INTO refinement_bindings VALUES (?,?)',(run['id'],_encoded(binding)))
            return binding

    def refinement_update_receipt(self, run_id, body):
        from .refinement_plan import RefinementError
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM refinement_updates WHERE update_id=?",
                                     (body["update_id"],)).fetchone()
        if row is None:
            return None
        if row["run_id"] != run_id or row["request_digest"] != _digest(body):
            raise RefinementError("此计划更新身份已用于不同内容或任务。", "refinement_update_conflict", 409)
        return json.loads(row["result"])

    def update_refinement(self, run_id, body, observations, *, expected_scene):
        from .refinement_plan import RefinementError, apply_update, selection_identity, validate_update
        body = validate_update(body)
        with self._write() as connection:
            row = connection.execute("SELECT * FROM refinement_updates WHERE update_id=?", (body["update_id"],)).fetchone()
            if row:
                if row["run_id"] != run_id or row["request_digest"] != _digest(body):
                    raise RefinementError("同更新身份内容冲突。", "refinement_update_conflict", 409)
                return json.loads(row["result"])
            plan_row = connection.execute("SELECT document FROM refinement_plans WHERE singleton=1").fetchone()
            binding = connection.execute("SELECT binding FROM refinement_bindings WHERE run_id=?", (run_id,)).fetchone()
            if plan_row is None or binding is None:
                raise RefinementError("此任务尚未绑定持久计划。", "refinement_unbound", 409)
            plan = json.loads(plan_row[0])
            if plan["version"] != body["expected_version"]:
                raise RefinementError("计划版本已变化，请读取当前版本后重新决定。", "refinement_version_conflict", 409,
                                      current_version=plan["version"])
            if json.loads(binding[0])["goal"]["id"] != plan["goal_id"]:
                raise RefinementError("本任务目标已被新执行请求替代。", "refinement_goal_changed", 409)
            project = json.loads(self._read_row(connection)["manifest"])
            if selection_identity(project.get("current_scene")) != expected_scene:
                raise RefinementError("证据校验期间场景已变化。", "refinement_reference_invalid", 409)
            updated = apply_update(plan, body, observations)
            encoded = _encoded(updated)
            connection.execute("UPDATE refinement_plans SET document=? WHERE singleton=1", (encoded,))
            connection.execute("INSERT INTO refinement_plan_versions VALUES (?,?)", (updated["version"], encoded))
            result = {"plan_id": updated["id"], "version": updated["version"], "goal_id": updated["goal_id"],
                      "item_ids": [c["item_id"] for c in body.get("changes", [])]}
            connection.execute("INSERT INTO refinement_updates VALUES (?,?,?,?)",
                               (body["update_id"], run_id, _digest(body), _encoded(result)))
            return result

    def local_round(self, run_id, *, round_id=None):
        """Read one authoritative round without creating or changing one."""
        with self._connection() as connection:
            if round_id is None:
                row = connection.execute(
                    "SELECT document FROM local_rounds WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT document FROM local_rounds WHERE run_id=? AND round_id=?",
                    (run_id, round_id)).fetchone()
        return json.loads(row["document"]) if row else None

    def begin_local_round(self, document):
        from .refinement_round import RoundError, criteria_snapshot, digest
        with self._write() as connection:
            project_row = self._read_row(connection)
            if project_row is None:
                raise RoundError("项目SQL状态不可用。", "round_working_changed", 409)
            project = json.loads(project_row["manifest"])
            start = document["start"]
            if (project["revision"] != start["project_revision"]
                    or project["workspace"]["working_version"] != start["working_version"]
                    or project.get("current_scene") != start["selection"]):
                raise RoundError("Working已变化，不能在旧基线上开始局部轮。", "round_working_changed", 409)
            row = connection.execute("SELECT document FROM refinement_plans WHERE singleton=1").fetchone()
            binding = connection.execute("SELECT binding FROM refinement_bindings WHERE run_id=?",
                                         (document["run_id"],)).fetchone()
            if row is None or binding is None:
                raise RoundError("Plan或共同目标尚未绑定。", "round_goal_changed", 409)
            plan, goal = json.loads(row["document"]), json.loads(binding["binding"])
            if (plan["goal_id"] != document["goal_id"] or goal["goal"]["id"] != document["goal_id"]
                    or digest(criteria_snapshot(plan, document["item_ids"])) != document["criteria_digest"]):
                raise RoundError("当前目标或标准已改变。", "round_criteria_changed", 409)
            open_row = connection.execute(
                "SELECT round_id FROM local_rounds WHERE status IN ('active','paused') LIMIT 1").fetchone()
            if open_row is not None:
                raise RoundError("已有未结束局部轮。", "round_not_resumable", 409)
            connection.execute("INSERT INTO local_rounds VALUES (?,?,?,?,?,?)",
                               (document["id"], document["run_id"], document["status"],
                                document["version"], _encoded(document), document["created_at"]))
            return copy.deepcopy(document)

    def update_local_round(self, document, expected_version, *, status=None):
        from .refinement_round import RoundError
        with self._write() as connection:
            row = connection.execute("SELECT run_id,status,version,document FROM local_rounds WHERE round_id=?",
                                     (document["id"],)).fetchone()
            if row is None or row["run_id"] != document["run_id"]:
                raise RoundError("局部轮不存在。", "round_not_resumable", 409)
            if row["version"] != expected_version or (status is not None and row["status"] != status):
                raise RoundError("局部轮版本或状态已改变。", "round_version_conflict", 409,
                                 current_version=row["version"])
            updated = copy.deepcopy(document)
            updated["version"] = expected_version + 1
            connection.execute("UPDATE local_rounds SET document=?,version=?,status=? WHERE round_id=?",
                               (_encoded(updated), updated["version"], updated["status"], updated["id"]))
            return updated

    def local_resume(self, resume_id):
        """A missing dispatch marker on a legacy receipt has unknown outcome."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT r.run_id,r.round_id,r.request_digest,r.result,d.state"
                " FROM local_round_resumes r LEFT JOIN local_round_dispatches d"
                " ON d.resume_id=r.resume_id WHERE r.resume_id=?", (resume_id,)).fetchone()
        return ({"run_id": row["run_id"], "round_id": row["round_id"],
                 "request_digest": row["request_digest"], "result": json.loads(row["result"]),
                 "dispatch_state": row["state"] or "unknown"} if row else None)

    def reserve_local_resume(self, run_id, round_id, body):
        from .refinement_round import RoundError, digest
        request_digest = digest(body)
        with self._write() as connection:
            prior = connection.execute(
                "SELECT r.run_id,r.round_id,r.request_digest,r.result,d.state"
                " FROM local_round_resumes r LEFT JOIN local_round_dispatches d"
                " ON d.resume_id=r.resume_id WHERE r.resume_id=?", (body["resume_id"],)).fetchone()
            if prior:
                if (prior["run_id"] != run_id or prior["round_id"] != round_id
                        or prior["request_digest"] != request_digest):
                    raise RoundError("续做身份已用于不同请求。", "round_resume_conflict", 409)
                return json.loads(prior["result"]), False, prior["state"] or "unknown"
            row = connection.execute("SELECT status,version,document FROM local_rounds WHERE round_id=?"
                                     " AND run_id=?", (round_id, run_id)).fetchone()
            if row is None or row["status"] != "paused":
                raise RoundError("当前轮不可续做。", "round_not_resumable", 409)
            if row["version"] != body["expected_round_version"]:
                raise RoundError("轮版本已改变。", "round_version_conflict", 409,
                                 current_version=row["version"])
            updated = json.loads(row["document"])
            updated["status"] = "active"
            updated["version"] += 1
            result = {"resume_id": body["resume_id"], "round_id": round_id,
                      "round_version": updated["version"], "run_id": run_id}
            connection.execute("UPDATE local_rounds SET document=?,status='active',version=?"
                               " WHERE round_id=?", (_encoded(updated), updated["version"], round_id))
            connection.execute("INSERT INTO local_round_resumes VALUES (?,?,?,?,?)",
                               (body["resume_id"], run_id, round_id, request_digest, _encoded(result)))
            connection.execute("INSERT INTO local_round_dispatches VALUES (?,'accepted',NULL)",
                               (body["resume_id"],))
            return result, True, "accepted"

    def mark_local_resume_started(self, run_id, resume_id):
        """This is the worker's first durable action, before Provider or Blender."""
        from .refinement_round import RoundError
        with self._write() as connection:
            row = connection.execute(
                "SELECT r.round_id,d.state FROM local_round_resumes r"
                " LEFT JOIN local_round_dispatches d ON d.resume_id=r.resume_id"
                " WHERE r.resume_id=? AND r.run_id=?", (resume_id, run_id)).fetchone()
            if row is None or row["state"] is None:
                raise RoundError("续做调度状态未知。", "round_resume_unknown", 409)
            if row["state"] == "started":
                return False
            round_row = connection.execute(
                "SELECT status FROM local_rounds WHERE round_id=? AND run_id=?",
                (row["round_id"], run_id)).fetchone()
            if round_row is None or round_row["status"] != "active":
                raise RoundError("续做轮状态已改变。", "round_not_resumable", 409)
            connection.execute(
                "UPDATE local_round_dispatches SET state='started',started_at=? WHERE resume_id=?",
                (_now(), resume_id))
            return True

    def end_local_round(self, run_id, body):
        """Metadata-only CAS end; damaged evidence and unknown writes stay in history."""
        from .refinement_round import RoundError, digest
        with self._write() as connection:
            row = connection.execute(
                "SELECT status,version,document FROM local_rounds WHERE round_id=? AND run_id=?",
                (body["round_id"], run_id)).fetchone()
            if row is None:
                raise RoundError("局部轮不存在。", "round_not_resumable", 409)
            document = json.loads(row["document"])
            identity = digest(body)
            if row["status"] == "ended":
                if document.get("end_id") == body["end_id"] and document.get("end_digest") == identity:
                    return {"round_id": body["round_id"], "version": row["version"], "status": "ended"}
                raise RoundError("局部轮已经由其他操作结束。", "round_end_conflict", 409)
            if row["status"] not in {"active", "paused"}:
                raise RoundError("局部轮已经正式保存。", "round_not_resumable", 409)
            if row["version"] != body["expected_round_version"]:
                raise RoundError("轮版本已改变。", "round_version_conflict", 409,
                                 current_version=row["version"])
            document["status"] = "ended"
            document["version"] += 1
            document["end_reason"] = body["reason"]
            document["end_id"] = body["end_id"]
            document["end_digest"] = identity
            connection.execute(
                "UPDATE local_rounds SET document=?,status='ended',version=? WHERE round_id=?",
                (_encoded(document), document["version"], body["round_id"]))
            return {"round_id": body["round_id"], "version": document["version"], "status": "ended"}

    def register_evaluation_input(self, manifest):
        """Register an open evaluation input manifest; idempotent for same content."""
        input_id = manifest.get("input_id") if isinstance(manifest, dict) else None
        if not isinstance(input_id, str) or not input_id:
            raise StateStoreError("缺少评审输入身份。", "evaluation_input_invalid", 400)
        from .evaluation_input import EvaluationInputError, validate_manifest
        try:
            validate_manifest(manifest)
        except EvaluationInputError as error:
            raise StateStoreError(str(error), error.code, 409) from None
        digest = manifest["input_digest"]
        with self._write() as connection:
            existing = connection.execute(
                "SELECT input_digest,state FROM evaluation_inputs WHERE input_id=?",
                (input_id,)).fetchone()
            if existing:
                if existing["input_digest"] != digest:
                    raise StateStoreError("相同输入 ID 对应不同内容。", "evaluation_input_conflict", 409)
                return input_id, False
            connection.execute(
                "INSERT INTO evaluation_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (input_id, manifest["evaluation_id"], manifest["run_id"],
                 manifest["attempt_id"], manifest["kind"], manifest["project_id"],
                 digest, _encoded(manifest), "open", None, None, _now()))
            return input_id, True

    def update_evaluation_input(self, manifest, *, expected_digest):
        """CAS append/return-state update of an open, persisted manifest."""
        from .evaluation_input import EvaluationInputError, validate_manifest
        try:
            validate_manifest(manifest)
        except EvaluationInputError as error:
            raise StateStoreError(str(error), error.code, 409) from None
        with self._write() as connection:
            row = connection.execute(
                "SELECT manifest,input_digest,state FROM evaluation_inputs WHERE input_id=?",
                (manifest["input_id"],)).fetchone()
            if row is None or row["state"] != "open":
                raise StateStoreError("评审输入未开放。", "evaluation_input_not_open", 409)
            if row["input_digest"] != expected_digest:
                raise StateStoreError("评审输入并发改变。", "evaluation_input_conflict", 409)
            prior = json.loads(row["manifest"])
            if any(prior.get(key) != manifest.get(key) for key in (
                    "evaluation_id", "input_id", "run_id", "attempt_id", "kind", "project_id", "context")):
                raise StateStoreError("评审输入身份变化。", "evaluation_input_conflict", 409)
            if (len(manifest["items"]) < len(prior["items"])
                    or len(manifest["requests"]) < len(prior["requests"])):
                raise StateStoreError("评审发送历史不能缩短。", "evaluation_input_conflict", 409)
            if manifest["items"][:len(prior["items"])] != prior["items"]:
                # Sent requests may be appended to prior rows, but evidence identity/content is immutable.
                for before, after in zip(prior["items"], manifest["items"]):
                    if {k: v for k, v in before.items() if k != "sent_requests"} != {
                            k: v for k, v in after.items() if k != "sent_requests"}:
                        raise StateStoreError("评审输入内容变化。", "evaluation_input_conflict", 409)
                    if after["sent_requests"][:len(before.get("sent_requests", []))] != before.get("sent_requests", []):
                        raise StateStoreError("发送事实被改写。", "evaluation_input_conflict", 409)
            for before, after in zip(prior["requests"], manifest["requests"]):
                if (before["sequence"] != after["sequence"]
                        or before["payload_digest"] != after["payload_digest"]
                        or before["state"] == "returned" and after["state"] != "returned"):
                    raise StateStoreError("评审请求事实被改写。", "evaluation_input_conflict", 409)
            connection.execute(
                "UPDATE evaluation_inputs SET manifest=?,input_digest=? WHERE input_id=?",
                (_encoded(manifest), manifest["input_digest"], manifest["input_id"]))
            return manifest["input_id"]

    def seal_evaluation_input(self, input_id, *, final_state, manifest=None):
        """Seal the input; further mutation is rejected.  Idempotent for same state."""
        if final_state not in {"completed", "failed", "cancelled", "unknown"}:
            raise StateStoreError(f"无效封存状态：{final_state}", "evaluation_input_invalid", 400)
        with self._write() as connection:
            row = connection.execute(
                "SELECT state,seal_state,manifest FROM evaluation_inputs WHERE input_id=?",
                (input_id,)).fetchone()
            if row is None:
                raise StateStoreError("评审输入未登记。", "evaluation_input_missing", 409)
            if row["state"] == "sealed":
                if row["seal_state"] == final_state:
                    return json.loads(row["manifest"]), False
                raise StateStoreError("评审输入已封存为不同状态。", "evaluation_input_conflict", 409)
            if row["state"] == "abandoned":
                raise StateStoreError("评审输入已废弃。", "evaluation_input_conflict", 409)
            if manifest is None:
                raise StateStoreError("封存缺少最终清单。", "evaluation_input_invalid", 409)
            from .evaluation_input import EvaluationInputError, validate_manifest
            try:
                validate_manifest(manifest)
            except EvaluationInputError as error:
                raise StateStoreError(str(error), error.code, 409) from None
            if (manifest["input_id"] != input_id or manifest.get("seal_state") != final_state
                    or not manifest.get("sealed")):
                raise StateStoreError("封存身份不一致。", "evaluation_input_mismatch", 409)
            prior = json.loads(row["manifest"])
            if any(prior.get(key) != manifest.get(key) for key in (
                    "evaluation_id", "input_id", "run_id", "attempt_id", "kind", "project_id", "context", "items", "requests")):
                raise StateStoreError("封存前清单已变化。", "evaluation_input_mismatch", 409)
            sealed_manifest = _encoded(manifest)
            connection.execute(
                "UPDATE evaluation_inputs SET state='sealed',seal_state=?,manifest=?,sealed_at=?"
                " WHERE input_id=?",
                (final_state, sealed_manifest, _now(), input_id))
            return json.loads(sealed_manifest), True

    def lookup_evaluation_input(self, input_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT manifest,state,seal_state,input_digest FROM evaluation_inputs"
                " WHERE input_id=?", (input_id,)).fetchone()
            if row is None:
                return None
            return {"manifest": json.loads(row["manifest"]), "state": row["state"],
                    "seal_state": row["seal_state"], "input_digest": row["input_digest"]}

    def refinement_evaluations(self):
        """One read snapshot, including refused results; no mutable acceptance copy."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT r.receipt,i.manifest FROM evaluation_receipts r"
                " JOIN evaluation_inputs i ON r.input_id=i.input_id"
                " ORDER BY r.created_at,r.rowid").fetchall()
            return [(json.loads(r["receipt"]), json.loads(r["manifest"])) for r in rows]

    def evaluation_receipts_for_run(self, run_id, *, connection=None):
        if connection is None:
            with self._connection() as opened:
                return self.evaluation_receipts_for_run(run_id, connection=opened)
        rows = connection.execute(
            "SELECT receipt FROM evaluation_receipts WHERE run_id=? ORDER BY created_at,evaluation_id",
            (run_id,)).fetchall()
        return [json.loads(row["receipt"]) for row in rows]

    def lookup_evaluation_receipt(self, evaluation_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt,input_digest,result_digest FROM evaluation_receipts"
                " WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if row is None:
                return None
            return json.loads(row["receipt"])

    def register_evaluation_receipt(self, receipt_data):
        """Register an evaluation receipt without modifying the workspace."""
        evaluation_id = receipt_data.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise StateStoreError("缺少评审身份。", "evaluation_input_invalid", 400)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT receipt,input_digest,result_digest FROM evaluation_receipts"
                " WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if existing:
                if (existing["input_digest"] != receipt_data.get("input_digest")
                        or existing["result_digest"] != receipt_data.get("result_digest")):
                    raise StateStoreError("相同评审 ID 对应不同输入或结果。",
                                          "evaluation_conflict", 409)
                return json.loads(existing["receipt"]), False
            self._check_new_evaluation_receipt(connection, receipt_data)
            timestamp = _now()
            record = {**copy.deepcopy(receipt_data), "created_at": timestamp}
            connection.execute(
                "INSERT INTO evaluation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation_id, receipt_data["input_id"], receipt_data["input_digest"],
                 receipt_data["result_digest"], receipt_data["run_id"],
                 receipt_data["project_id"], receipt_data.get("verdict"),
                 _encoded(receipt_data.get("scope")), receipt_data["application_status"],
                 receipt_data.get("application_reason"), _encoded(record), timestamp))
            return record, True

    def prepare_operation(self, operation):
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise StateStoreError("缺少稳定操作身份。", "invalid_operation", 400)
        identity = operation.get("identity")
        if not isinstance(identity, dict):
            raise StateStoreError("操作准备缺少起始身份。", "invalid_operation", 400)
        if identity.get("project_id") != self.project_id:
            raise StateStoreError("操作项目身份不符。", "invalid_operation", 400)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT identity,state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["identity"] != _encoded(identity):
                    raise StateStoreError("相同操作 ID 对应不同的起始内容。", "operation_conflict", 409)
                return existing["state"]
            timestamp = _now()
            connection.execute(
                "INSERT INTO operations(operation_id,project_id,run_id,kind,identity,state,created_at,updated_at)"
                " VALUES (?,?,?,?,?,'prepared',?,?)",
                (operation_id, self.project_id, identity.get("run_id"),
                 identity.get("kind", "unknown"), _encoded(identity), timestamp, timestamp))
            return "prepared"

    def mark_unknown(self, operation_id):
        with self._write() as connection:
            connection.execute(
                "UPDATE operations SET state='unknown',updated_at=? WHERE operation_id=? AND state='prepared'",
                (_now(), operation_id))

    def lookup_receipt(self, operation_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt,command_digest FROM receipts WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                receipt = json.loads(row["receipt"])
                artifacts = receipt.get("artifacts") or []
                failures = self.verify_artifacts(artifacts) if artifacts else []
                return {"status": "committed", "receipt": receipt,
                        "command_digest": row["command_digest"],
                        "artifact_status": ("unavailable" if failures else
                                            "available" if artifacts else "unknown"),
                        "artifact_failures": failures}
            operation = connection.execute(
                "SELECT state,identity FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if operation:
                return {"status": operation["state"], "identity": json.loads(operation["identity"])}
            return {"status": "missing"}

    def revision_mapping(self, run_id, legacy_revision_id):
        revision_id = self.global_revision_id(self.project_id, run_id, legacy_revision_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id,legacy_revision_id,selection,artifacts,availability"
                " FROM scene_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            return None
        return {"global_revision_id": revision_id, "run_id": row["run_id"],
                "legacy_revision_id": row["legacy_revision_id"],
                "selection": json.loads(row["selection"]),
                "artifacts": json.loads(row["artifacts"]),
                "availability": row["availability"]}

    def legacy_run_projection(self, run_id):
        """Read the verified migration snapshot, never the mutable Run JSON."""
        if run_id in self._legacy_projection_cache:
            return copy.deepcopy(self._legacy_projection_cache[run_id])
        if not self._legacy_backup_loaded:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT detail FROM reconciliation WHERE key='migration_backup'").fetchone()
            self._legacy_backup_manifest = json.loads(row["detail"]) if row else None
            self._legacy_backup_loaded = True
        result = {"commits": [], "initial_working_commit": None,
                  "working_scene": None, "expected_project_revision": None,
                  "trusted": False}
        backup = self._legacy_backup_manifest
        if isinstance(backup, dict):
            item = next((entry for entry in backup.get("run_files", [])
                         if entry.get("source") == f"runs/{run_id}/run.json"), None)
            if item:
                path = self.metadata / "state-migration-backup" / "runs" / run_id / "run.json"
                try:
                    _not_link(path.parent)
                    _not_link(path)
                    if path.is_file() and _file_hash(path) == item["sha256"]:
                        saved = json.loads(path.read_text(encoding="utf-8"))
                        if (isinstance(saved, dict) and saved.get("id") == run_id
                                and saved.get("project_id") == self.project_id):
                            rows = saved.get("commits")
                            if (isinstance(rows, list) and all(
                                    isinstance(row, dict) and
                                    isinstance(row.get("revision_id"), str) and
                                    type(row.get("working_version")) is int and
                                    type(row.get("project_revision")) is int for row in rows)):
                                keys = [(row["revision_id"], row["project_revision"]) for row in rows]
                                if len(keys) == len(set(keys)):
                                    result.update(commits=copy.deepcopy(rows), trusted=True,
                                                  initial_working_commit=copy.deepcopy(
                                                      saved.get("initial_working_commit")),
                                                  working_scene=copy.deepcopy(saved.get("working_scene")),
                                                  expected_project_revision=saved.get(
                                                      "expected_project_revision"))
                except (OSError, ValueError, KeyError, StateStoreError):
                    pass
        self._legacy_projection_cache[run_id] = result
        return copy.deepcopy(result)

    def receipts_for_run(self, run_id, *, connection=None):
        if connection is None:
            with self._connection() as opened:
                return self.receipts_for_run(run_id, connection=opened)
        rows = connection.execute(
            "SELECT receipt FROM receipts WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return [json.loads(row["receipt"]) for row in rows]

    def run_counter(self, run_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT saved_edit_rounds,needs_reconciliation FROM run_counters WHERE run_id=?",
                (run_id,)).fetchone()
            return {"saved_edit_rounds": row["saved_edit_rounds"] if row else 0,
                    "needs_reconciliation": bool(row["needs_reconciliation"]) if row else False}

    def operation_state(self, operation_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state,edit_retry_count,identity FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if row is None:
                return None
            attempts = connection.execute(
                "SELECT attempt_id,retry_index,status,failure_category,retryable,detail "
                "FROM edit_attempts WHERE operation_id=? ORDER BY rowid", (operation_id,)).fetchall()
            return {"state": row["state"], "edit_retry_count": row["edit_retry_count"],
                    "identity": json.loads(row["identity"]),
                    "attempts": [dict(item) for item in attempts]}

    def local_edit_attempt_count(self, run_id, *, round_id=None):
        with self._connection() as connection:
            if round_id is None:
                return connection.execute(
                    "SELECT COUNT(*) FROM edit_attempts AS a JOIN operations AS o"
                    " ON o.operation_id=a.operation_id WHERE o.run_id=? AND o.kind IN ('draft_edit','asset_import','asset_transform')",
                    (run_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT o.identity,COUNT(*) AS attempts FROM edit_attempts AS a"
                " JOIN operations AS o ON o.operation_id=a.operation_id"
                " WHERE o.run_id=? AND o.kind IN ('draft_edit','asset_import','asset_transform') GROUP BY o.operation_id",
                (run_id,)).fetchall()
            return sum(row["attempts"] for row in rows
                       if json.loads(row["identity"]).get("round_id") == round_id)

    def readiness(self, run_id):
        """Policy state only; the existing 1-6 attempt driver remains unchanged."""
        loaded = self.load()
        if loaded is None:
            raise StateStoreError("项目状态尚未完成迁移。", "state_unavailable", 409)
        manifest = loaded[0]
        run = self.run_counter(run_id)
        saved = run["saved_edit_rounds"]
        since = manifest["workspace"].get("rounds_since_judge")
        return {"saved_edit_rounds": saved, "rounds_since_judge": since,
                "saved_limit_reached": saved is None or saved >= 12,
                "judge_required": since is None or since >= 3,
                "needs_reconciliation": run["needs_reconciliation"] or since is None,
                "may_start_edit": saved is not None and saved < 12 and since is not None and since < 3}

    def begin_attempt(self, operation_id, attempt_id, *, retry=False,
                      local_limits=None, round_id=None):
        """Persist a unique scheduled attempt before any external execution."""
        if not isinstance(attempt_id, str) or not attempt_id:
            raise StateStoreError("缺少尝试身份。", "invalid_attempt", 400)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT operation_id,attempt_id,retry_index,status FROM edit_attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            if existing:
                if existing["operation_id"] != operation_id:
                    raise StateStoreError("尝试 ID 已属于另一操作。", "attempt_conflict", 409)
                return dict(existing), False
            row = connection.execute(
                "SELECT state,edit_retry_count,run_id,kind,identity FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if row is None or row["state"] != "prepared":
                raise StateStoreError("操作未准备或结果待核对，不能调度。", "operation_not_schedulable", 409)
            from .agent_resources import attempt_limits
            local_limits = attempt_limits(connection, row, round_id, local_limits)
            if local_limits is not None:
                from .refinement_round import RoundError
                identity = json.loads(row["identity"])
                if (row["kind"] not in {"draft_edit", "asset_import", "asset_transform"} or not row["run_id"] or
                        identity.get("round_id") != round_id):
                    raise RoundError("草稿尝试的轮身份不匹配。", "round_budget_invalid", 409)
                counts = connection.execute(
                    "SELECT o.identity,COUNT(*) AS attempts FROM edit_attempts AS a"
                    " JOIN operations AS o ON o.operation_id=a.operation_id"
                    " WHERE o.run_id=? AND o.kind IN ('draft_edit','asset_import','asset_transform') GROUP BY o.operation_id",
                    (row["run_id"],)).fetchall()
                run_attempts = sum(item["attempts"] for item in counts)
                round_attempts = sum(item["attempts"] for item in counts
                                     if json.loads(item["identity"]).get("round_id") == round_id)
                if run_attempts >= local_limits["max_edit_attempts"]:
                    raise RoundError("本Run草稿总尝试额度已用完。", "round_resource_exhausted", 409)
                if round_attempts >= local_limits["max_edit_attempts_per_round"]:
                    raise RoundError("本局部轮草稿尝试额度已用完；仍可自检或提交已确认草稿。",
                                     "round_resource_exhausted", 409)
            last = connection.execute(
                "SELECT status,retryable FROM edit_attempts WHERE operation_id=? ORDER BY rowid DESC LIMIT 1",
                (operation_id,)).fetchone()
            if retry:
                if row["kind"] == "asset_import":
                    raise StateStoreError("资产导入不自动重试。", "retry_not_allowed", 409)
                if last is None or last["status"] != "failed" or not last["retryable"]:
                    raise StateStoreError("上次结果不能安全重试。", "retry_not_allowed", 409)
                if row["edit_retry_count"] >= 5:
                    raise StateStoreError("本编辑计划五次重试已用完。", "retry_limit", 409)
                retry_index = row["edit_retry_count"] + 1
                connection.execute(
                    "UPDATE operations SET edit_retry_count=?,updated_at=? WHERE operation_id=?",
                    (retry_index, _now(), operation_id))
            else:
                if last is not None:
                    raise StateStoreError("首次执行已登记，不能换 ID 重开。", "attempt_conflict", 409)
                retry_index = 0
            connection.execute(
                "INSERT INTO edit_attempts(attempt_id,operation_id,retry_index,status,created_at)"
                " VALUES (?,?,?,'scheduled',?)",
                (attempt_id, operation_id, retry_index, _now()))
            return {"operation_id": operation_id, "attempt_id": attempt_id,
                    "retry_index": retry_index, "status": "scheduled"}, True

    def record_attempt_result(self, attempt_id, *, outcome, failure_category=None,
                              confirmed_no_commit=False, detail=None):
        """Only an explicit confirmed no-commit failure can be retryable."""
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise StateStoreError("尝试结果无效。", "invalid_attempt", 400)
        safe_categories = {"confirmed_no_write", "confirmed_rejected"}
        forbidden = {"version_conflict", "commit_unknown", "integrity_failure", "write_unknown"}
        if failure_category in forbidden:
            confirmed_no_commit = False
        retryable = bool(outcome == "failed" and confirmed_no_commit
                         and failure_category in safe_categories)
        with self._write() as connection:
            row = connection.execute(
                "SELECT operation_id,status,failure_category,retryable,detail FROM edit_attempts"
                " WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise StateStoreError("尝试尚未登记。", "attempt_missing", 409)
            if row["status"] != "scheduled":
                if (row["status"] == outcome and row["failure_category"] == failure_category
                        and bool(row["retryable"]) == retryable and row["detail"] == detail):
                    return {"status": outcome, "retryable": retryable}, False
                raise StateStoreError("同一尝试出现冲突结果。", "attempt_conflict", 409)
            connection.execute(
                "UPDATE edit_attempts SET status=?,failure_category=?,retryable=?,detail=?"
                " WHERE attempt_id=?",
                (outcome, failure_category, int(retryable), detail, attempt_id))
            if outcome == "unknown":
                connection.execute(
                    "UPDATE operations SET state='unknown',updated_at=? WHERE operation_id=?",
                    (_now(), row["operation_id"]))
            return {"status": outcome, "retryable": retryable}, True

    def commit_scene(self, command, updated_manifest, receipt, *, expected_generation):
        """Atomically publish revision, head, receipt and 12/3/5 counter transition."""
        operation_id = command["operation_id"]
        command_digest = _digest(command)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT command_digest,receipt FROM receipts WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["command_digest"] != command_digest:
                    raise StateStoreError("相同操作 ID 不能提交另一份内容。", "operation_conflict", 409)
                return json.loads(existing["receipt"]), False
            operation = connection.execute(
                "SELECT identity,state,edit_retry_count FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if operation is None:
                raise StateStoreError("操作未在外部写入前准备。", "operation_missing", 409)
            if operation["identity"] != _encoded(command["identity"]):
                raise StateStoreError("操作准备与提交内容冲突。", "operation_conflict", 409)
            if operation["state"] == "unknown":
                raise StateStoreError("外部写结果未知，先核对原回执与文件。", "operation_unknown", 409)
            row = self._read_row(connection)
            if row is None or row["project_id"] != self.project_id:
                raise StateStoreError("项目状态尚未完成迁移。", "state_unavailable", 409)
            current = json.loads(row["manifest"])
            if (row["generation"] != expected_generation
                    or current.get("revision") != command["expected_project_revision"]
                    or current.get("current_scene") != command.get("source_selection")
                    or current.get("workspace", {}).get("working_version") != command["expected_working_version"]):
                raise StateStoreError("旧操作不能覆盖较新的 Working Scene。", "project_revision_conflict", 409)
            if (updated_manifest.get("id") != self.project_id
                    or updated_manifest.get("current_scene") != command.get("target_selection")
                    or updated_manifest.get("revision") != current["revision"] + 1
                    or updated_manifest.get("workspace", {}).get("working_version")
                    != current["workspace"]["working_version"] + 1):
                raise StateStoreError("提交后的项目状态转换无效。", "invalid_operation", 400)
            updated_manifest = copy.deepcopy(updated_manifest)
            receipt = copy.deepcopy(receipt)
            previous_since = current["workspace"].get("rounds_since_judge")
            previous_run = connection.execute(
                "SELECT saved_edit_rounds,needs_reconciliation FROM run_counters WHERE run_id=?",
                (command.get("run_id"),)).fetchone()
            saved_before = previous_run["saved_edit_rounds"] if previous_run else 0
            retry_before = operation["edit_retry_count"]
            builder = command["kind"] == "builder"
            if builder:
                from .agent_resources import enforce_save
                enforce_save(connection, command.get('run_id'))
            local = command.get("local_round")
            local_document = None
            if local is not None:
                from .refinement_round import RoundError, criteria_snapshot, digest
                if not builder or saved_before is None or saved_before >= 12:
                    raise RoundError("已达到正式保存上限或计数待核对。", "round_resource_exhausted", 409)
                if (previous_since is None or previous_since >= local["judge_trigger"]):
                    raise RoundError("提交前需先完成适用Judge。", "round_judge_required", 409)
                row = connection.execute("SELECT version,status,document FROM local_rounds WHERE round_id=?"
                                         " AND run_id=?", (local["id"], command["run_id"])).fetchone()
                if row is None or row["status"] != "active" or row["version"] != local["version"]:
                    raise RoundError("局部轮版本或状态已变化。", "round_version_conflict", 409,
                                     current_version=row["version"] if row else None)
                local_document = json.loads(row["document"])
                start = local_document["start"]
                if (start["selection"] != current.get("current_scene")
                        or start["project_revision"] != current["revision"]
                        or start["working_version"] != current["workspace"]["working_version"]
                        or local_document["commit_operation_id"] != operation_id
                        or local_document["selected_revision"] != command["legacy_revision_id"]
                        or local_document["pending_revision"] is not None):
                    raise RoundError("局部轮基线或最终草稿不一致。", "round_working_changed", 409)
                if (local_document["goal_id"] != local["goal_id"]
                        or local_document["criteria_digest"] != local["criteria_digest"]):
                    raise RoundError("局部轮目标/标准身份不一致。", "round_criteria_changed", 409)
                plan_row = connection.execute("SELECT document FROM refinement_plans WHERE singleton=1").fetchone()
                goal_row = connection.execute("SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1").fetchone()
                if plan_row is None or goal_row is None:
                    raise RoundError("目标或Plan缺失。", "round_goal_changed", 409)
                plan, goal = json.loads(plan_row["document"]), json.loads(goal_row["snapshot"])
                if goal["id"] != local_document["goal_id"] or plan["goal_id"] != goal["id"]:
                    raise RoundError("目标已变化。", "round_goal_changed", 409)
                if digest(criteria_snapshot(plan, local_document["item_ids"])) != local_document["criteria_digest"]:
                    raise RoundError("受评标准已变化。", "round_criteria_changed", 409)
                selected_steps = [step for step in local_document["steps"] if step["status"] == "selected"]
                if (not selected_steps or [step["id"] for step in selected_steps] != local["step_ids"]
                        or selected_steps[-1]["revision_id"] != local_document["selected_revision"]
                        or any(step["status"] in {"scheduled", "pending", "unknown", "needs_user_review"}
                               for step in local_document["steps"])):
                    raise RoundError("草稿步骤尚未确认或顺序不一致。", "round_operation_unknown", 409)
                for step in selected_steps:
                    attempt = connection.execute("SELECT status FROM edit_attempts WHERE attempt_id=?"
                                                 " AND operation_id=?", (step["attempt_id"], step["operation_id"])).fetchone()
                    if attempt is None or attempt["status"] != "succeeded":
                        raise RoundError("草稿执行回执缺失。", "round_operation_unknown", 409)
                retry_history = []
                for step in selected_steps:
                    retry_row = connection.execute("SELECT edit_retry_count FROM operations"
                                                   " WHERE operation_id=?", (step["operation_id"],)).fetchone()
                    retry_history.append({"operation_id": step["operation_id"],
                                          "successful_attempt_id": step["attempt_id"],
                                          "retry_count_before_settlement": retry_row["edit_retry_count"],
                                          "retry_count_after_settlement": 0})
                receipt["local_round"] = {"round_id": local_document["id"],
                                          "step_ids": local["step_ids"],
                                          "item_ids": local_document["item_ids"],
                                          "goal_id": local_document["goal_id"],
                                          "criteria_digest": local_document["criteria_digest"],
                                          "operations": retry_history}
            # Stage B records policy readiness. The existing bounded driver owns
            # scheduling until stage D; SQL still enforces CAS and operation safety.
            saved_after = (saved_before + 1 if saved_before is not None else None) if builder else saved_before
            since_after = (previous_since + 1 if previous_since is not None else None) if builder else previous_since
            retry_after = 0 if builder else retry_before
            updated_manifest["workspace"]["rounds_since_judge"] = since_after
            updated_manifest["workspace"]["rounds_since_judge_status"] = (
                "known" if since_after is not None else "needs_reconciliation")
            if builder:
                connection.execute(
                    "INSERT INTO run_counters(run_id,saved_edit_rounds,needs_reconciliation)"
                    " VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET saved_edit_rounds=excluded.saved_edit_rounds",
                    (command["run_id"], saved_after, int(saved_after is None)))
            receipt["counters"] = {
                "run_id": command.get("run_id"), "operation_id": operation_id,
                "saved_edit_rounds": {"before": saved_before, "after": saved_after},
                "rounds_since_judge": {"before": previous_since, "after": since_after},
                "edit_retry_count": {"before": retry_before, "after": retry_after},
            }
            self._after_step("counters")
            revision_id = receipt["global_revision_id"]
            mapping = connection.execute(
                "SELECT selection,artifacts,availability FROM scene_revisions WHERE revision_id=?",
                (revision_id,)).fetchone()
            if mapping is None:
                connection.execute(
                    "INSERT INTO scene_revisions VALUES (?,?,?,?,?,?,?)",
                    (revision_id, command.get("run_id"), command.get("legacy_revision_id"),
                     _encoded(command["target_selection"]), _encoded(command.get("artifacts", [])),
                     "available" if command.get("artifacts") else "unknown", receipt["committed_at"]))
            else:
                prior_selection = json.loads(mapping["selection"])
                if mapping["availability"] == "available":
                    if (prior_selection != command["target_selection"]
                            or (command.get("artifacts")
                                and json.loads(mapping["artifacts"]) != command["artifacts"])):
                        raise StateStoreError("全局修订身份与既有产物冲突。", "revision_conflict", 409)
                elif (prior_selection.get("run_id") != command["target_selection"]["run_id"]
                      or prior_selection.get("revision_id") != command["target_selection"]["revision_id"]):
                    raise StateStoreError("历史修订映射身份冲突。", "revision_conflict", 409)
                elif command.get("artifacts"):
                    connection.execute(
                        "UPDATE scene_revisions SET selection=?,artifacts=?,availability='available'"
                        " WHERE revision_id=?",
                        (_encoded(command["target_selection"]),
                         _encoded(command["artifacts"]), revision_id))
            self._after_step("revision")
            connection.execute("UPDATE project_state SET manifest=?,generation=? WHERE singleton=1",
                               (_encoded(updated_manifest), expected_generation + 1))
            self._after_step("head")
            connection.execute(
                "INSERT INTO receipts(operation_id,project_id,run_id,kind,command_digest,receipt,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (operation_id, self.project_id, command.get("run_id"), command["kind"],
                 command_digest, _encoded(receipt), receipt["committed_at"]))
            self._after_step("receipt")
            connection.execute(
                "UPDATE operations SET state='committed',edit_retry_count=?,updated_at=?"
                " WHERE operation_id=?", (retry_after, _now(), operation_id))
            self._after_step("operation")
            if local_document is not None:
                # The round, Plan fact references and original scene receipt
                # succeed or roll back together. Rejected steps stay historical.
                for step in selected_steps:
                    connection.execute("UPDATE operations SET state='committed',edit_retry_count=0,updated_at=?"
                                       " WHERE operation_id=? AND state='prepared'",
                                       (_now(), step["operation_id"]))
                for item in plan["items"]:
                    if item["id"] in local_document["item_ids"] and operation_id not in item["operation_ids"]:
                        item["operation_ids"].append(operation_id)
                plan["version"] += 1
                encoded_plan = _encoded(plan)
                connection.execute("UPDATE refinement_plans SET document=? WHERE singleton=1", (encoded_plan,))
                connection.execute("INSERT INTO refinement_plan_versions VALUES (?,?)",
                                   (plan["version"], encoded_plan))
                local_document["status"] = "committed"
                local_document["version"] += 1
                local_document["receipt_operation_id"] = operation_id
                local_document["commit_receipt"] = {"operation_id": operation_id,
                    "project_revision": receipt["project_revision"],
                    "working_version": receipt["working_version"],
                    "saved_edit_rounds": saved_after, "rounds_since_judge": since_after}
                connection.execute("UPDATE local_rounds SET document=?,status='committed',version=?"
                                   " WHERE round_id=?", (_encoded(local_document),
                                                           local_document["version"], local_document["id"]))
                self._after_step("local_round")
            return receipt, True

    def accept_judge(self, judgement_id, selection, working_version):
        """Idempotently advance only an applicable current-head Judge watermark."""
        with self._write() as connection:
            previous = connection.execute(
                "SELECT judgement_id FROM accepted_judges WHERE judgement_id=?",
                (judgement_id,)).fetchone()
            if previous:
                return False
            row = self._read_row(connection)
            if row is None:
                raise StateStoreError("项目状态不可用。", "state_unavailable", 409)
            manifest = json.loads(row["manifest"])
            if (manifest.get("current_scene") != selection or
                    manifest["workspace"].get("working_version") != working_version):
                return False
            timestamp = _now()
            connection.execute(
                "INSERT INTO accepted_judges VALUES (?,?,?,?)",
                (judgement_id, _encoded(selection), working_version, timestamp))
            manifest["workspace"]["rounds_since_judge"] = 0
            manifest["workspace"]["rounds_since_judge_status"] = "known"
            manifest["workspace"]["judge_watermark"] = {
                "judgement_id": judgement_id, "selection": copy.deepcopy(selection),
                "working_version": working_version, "accepted_at": timestamp}
            connection.execute(
                "UPDATE project_state SET manifest=?,generation=? WHERE singleton=1",
                (_encoded(manifest), row["generation"] + 1))
            return True

    def _after_step(self, step):
        """Test seam for process-kill and transaction fault probes."""
        return None

    def reconciliation(self):
        with self._connection() as connection:
            rows = connection.execute("SELECT key,detail FROM reconciliation").fetchall()
            return {row["key"]: json.loads(row["detail"]) for row in rows}

    def active_head_status(self):
        """Report current head file availability without selecting another scene."""
        loaded = self.load()
        if loaded is None:
            return {"status": "unavailable", "reason": "state_unavailable"}
        selection = loaded[0].get("current_scene")
        if selection is None:
            return {"status": "empty"}
        revision_id = self.global_revision_id(
            self.project_id, selection["run_id"], selection["revision_id"])
        with self._connection() as connection:
            row = connection.execute(
                "SELECT selection,artifacts,availability FROM scene_revisions WHERE revision_id=?",
                (revision_id,)).fetchone()
        if row is None:
            return {"status": "unknown", "reason": "revision_mapping_missing"}
        if row["availability"] != "available":
            return {"status": "unknown", "reason": "legacy_artifacts_unknown"}
        artifacts = json.loads(row["artifacts"])
        failures = self.verify_artifacts(artifacts)
        return ({"status": "unavailable", "failures": failures} if failures
                else {"status": "available", "required_artifacts": len(artifacts)})

    def verify_artifacts(self, artifacts):
        """Check fixed required files outside any SQLite write transaction."""
        failures = []
        for item in artifacts:
            relative = item.get("path")
            if not isinstance(relative, str):
                failures.append({"path": relative, "reason": "invalid_path"})
                continue
            path = self.project_root / relative
            try:
                if (not path.is_relative_to(self.project_root)
                        or not path.resolve().is_relative_to(self.project_root)):
                    raise StateStoreError("文件越出项目目录。", "unsafe_project_path", 409)
                for parent in [path, *path.parents]:
                    if parent == self.project_root.parent:
                        break
                    _not_link(parent)
                if not path.is_file():
                    failures.append({"path": relative, "reason": "missing"})
                elif path.stat().st_size != item.get("size") or _file_hash(path) != item.get("sha256"):
                    failures.append({"path": relative, "reason": "changed"})
            except (OSError, StateStoreError):
                failures.append({"path": relative, "reason": "unavailable"})
        return failures
