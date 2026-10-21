"""Loopback-only HTTP app: UI, same-origin job API, and explicit artifact downloads."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .refinement_round import RoundError
from .runtime import AppError, RunManager
from .providers import ProviderError
from .data_lock import DataDirectoryInUse, acquire_process_lock
from .native_picker import _PICKER_LOCK, pick_native_path
from .bootstrap import ensure_env
from .config_store import ProfileError, ProfileRepository
from .credentials import CredentialError
from .project_store import ProjectError, ProjectRepository
from .asset_provider import AssetJobError

PROJECT = Path(__file__).resolve().parent.parent


def load_env(path):
    """Small dotenv reader: no interpolation, shell execution, or overwrite of OS env."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(name, value)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager, ui_root=None, profile_repository=None):
        self.manager = manager
        self.profile_repository = profile_repository or getattr(manager, "profile_repository", None)
        self.ui_root = Path(ui_root or PROJECT / "ui").resolve()
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "SceneLoop/0.4"

    def log_message(self, fmt, *args):
        # Request bodies, configured endpoints, environment values never enter access logs.
        pass

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.end_headers()

    def _json(self, value, status=200):
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(content))
        self.wfile.write(content)

    def _guard(self, mutation=False):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in {f"localhost:{port}", f"127.0.0.1:{port}"}:
            raise AppError("仅允许从本机工作台访问。", "invalid_host", 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise AppError("不允许跨站访问本地任务。", "invalid_origin", 403)
        if mutation and not secrets.compare_digest(self.headers.get("X-SceneLoop-Token", ""), self.server.csrf_token):
            raise AppError("会话已变更，请刷新工作台后重试。", "invalid_session", 403)

    def _body(self, limit=131072):
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise AppError("请求需要使用 JSON。", "content_type", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError("请求长度无效。") from None
        if not 0 < length <= limit:
            raise AppError("请求为空或超过本入口大小上限。", "body_size", 413)
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise AppError("JSON 请求格式无效。") from None

    def _file(self, path):
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._headers(200, content_type, path.stat().st_size)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 256):
                self.wfile.write(chunk)

    def _error(self, error):
        if isinstance(error, (AppError, RoundError, ProviderError, ProfileError, CredentialError, ProjectError, AssetJobError)):
            conflict = isinstance(error,AssetJobError) and any(word in error.code for word in ('conflict','drift','ended','not_ready'))
            self._json({"error": {"code": getattr(error, "code", "invalid_request"), "message": str(error),
                                  "outcome_unknown": bool(getattr(error, "outcome_unknown", False))}}, getattr(error, "status", None) or (409 if conflict else 400))
        else:
            self._json({"error": {"code": "internal_error", "message": "本地服务处理失败。请检查环境和已有任务记录。"}}, 500)

    def _profiles(self):
        repository = self.server.profile_repository
        if repository is None:
            raise AppError("当前服务尚未启用配置库，请重启新版后端。", "configuration_unavailable", 503)
        return repository

    def do_GET(self):
        try:
            self._guard()
            path = unquote(urlsplit(self.path).path)
            if path == "/api/health":
                repository = self.server.profile_repository
                self._json({**self.server.manager.health(), "csrf_token": self.server.csrf_token,
                            "configuration": {"available": repository is not None,
                                              "secret_storage": repository.storage_status() if repository else None}})
            elif path == "/api/profiles":
                repository = self._profiles()
                self._json({"profiles": repository.list(), "secret_storage": repository.storage_status()})
            elif path == "/api/projects":
                self._json({"projects": self.server.manager.list_projects()})
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})", path):
                self._json({"project": self.server.manager.get_project(match[1])})
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/refinement", path):
                self._json(self.server.manager.get_refinement(match[1]))
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/director-capability", path):
                from .director_creation import capability
                self._json({'capability':capability(self.server.manager,match[1])})
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/references/([0-9a-f]{32})", path):
                from .project_references import resolve
                state=self.server.manager._projects().state_store(match[1])
                with state._connection() as c:
                    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_references'").fetchone()
                    row=c.execute('SELECT document FROM project_references WHERE artifact_id=?',(match[2],)).fetchone() if exists else None
                if not row:raise AppError('项目参考不存在。','project_reference_missing',404)
                record=json.loads(row[0])
                _,image=resolve(self.server.manager,match[1],{k:record[k] for k in ('artifact_id','sha256','purpose')})
                self._file(image)
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/history", path):
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
                values = {key: rows[-1] for key, rows in query.items() if rows}
                if "version" in values:
                    try:
                        values["version"] = int(values["version"])
                    except ValueError:
                        raise AppError("历史版本筛选无效。")
                if "limit" in values:
                    try:
                        values["limit"] = int(values["limit"])
                    except ValueError:
                        raise AppError("历史查询数量无效。")
                self._json(self.server.manager.query_project_history(match[1], values))
            elif path == "/api/runs":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {"project_id", "limit", "before", "idempotency_key"} or any(len(rows) != 1 for rows in query.values()):
                    raise AppError("历史读取参数无效或重复。", "invalid_query")
                values = {key: rows[0] for key, rows in query.items()}
                project_id = values.get("project_id")
                if project_id is not None and not re.fullmatch(r"[0-9a-f]{32}", project_id):
                    raise AppError("项目筛选无效。", "invalid_project")
                raw_limit = values.get("limit", "100")
                if not re.fullmatch(r"[0-9]{1,3}", raw_limit) or not 1 <= int(raw_limit) <= 100:
                    raise AppError("历史查询数量必须为1–100。", "invalid_limit")
                limit = int(raw_limit)
                if "idempotency_key" in values:
                    if "before" in values:
                        raise AppError("提交恢复查询不能混用历史游标。", "invalid_query")
                    runs = self.server.manager.find_by_idempotency_key(project_id, values["idempotency_key"])
                    self._json({"runs": runs, "page": {"limit": limit, "has_more": False, "next_cursor": None}})
                else:
                    self._json(self.server.manager.list_page(project_id, limit=limit, before=values.get("before")))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/director/actions/([0-9a-f]{32})", path):
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                if any(len(rows)!=1 for rows in query.values()):
                    raise AppError('原动作查询参数重复。','invalid_query')
                self._json(self.server.manager.director_action_receipt(
                    match[1],match[2],{key:rows[0] for key,rows in query.items()}))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/director/timeline", path):
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                if set(query)-{'before','limit'} or any(len(rows)!=1 for rows in query.values()):
                    raise AppError('过程查询参数无效或重复。','invalid_query')
                raw=query.get('limit',['40'])[0]
                if not re.fullmatch(r'[0-9]{1,3}',raw) or not 1<=int(raw)<=100:
                    raise AppError('过程页数量必须为1–100。','director_timeline_limit_invalid')
                self._json(self.server.manager.director_timeline(match[1],before=query.get('before',[None])[0],limit=int(raw)))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})", path):
                self._json(self.server.manager.get(match[1]))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/local-round", path):
                self._json({"local_round": self.server.manager.get_local_round(match[1])})
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/asset-request", path):
                self._json({'asset_request':self.server.manager.get_asset_request(match[1])})
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/resource-request", path):
                self._json(self.server.manager.get_resource_request(match[1]))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/artifacts/(.+)", path):
                self._file(self.server.manager.artifact_path(match[1], match[2]))
            elif path.startswith("/api/"):
                raise AppError("接口不存在。", "not_found", 404)
            else:
                candidate = (self.server.ui_root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
                if not candidate.is_relative_to(self.server.ui_root) or not candidate.is_file():
                    raise AppError("页面不存在。", "not_found", 404)
                self._file(candidate)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(error)

    def do_POST(self):
        try:
            self._guard(mutation=True)
            path = urlsplit(self.path).path
            reference_upload=re.fullmatch(r"/api/projects/([0-9a-f]{32})/references",path)
            body = self._body(6*1024*1024 if reference_upload else 131072)
            if not isinstance(body, dict):
                raise AppError("请求必须是 JSON 对象。")
            if reference_upload:
                from .project_references import upload
                self._json(upload(self.server.manager,reference_upload[1],body),201)
            elif path == "/api/profiles/verify":
                self._json(self._profiles().verify(body.get("profile"), api_key=body.get("api_key"), expected_revision=body.get("expected_revision")))
            elif path == "/api/profiles":
                self._json({"profile": self._profiles().create(body.get("profile"), api_key=body.get("api_key"), verification_token=body.get("verification_token"))}, 201)
            elif match := re.fullmatch(r"/api/profiles/([A-Za-z0-9_-]{1,100})(?:/(delete|reveal))?", path):
                repository = self._profiles()
                identifier, action = match.groups()
                revision = body.get("expected_revision")
                if action == "delete":
                    repository.delete(identifier, revision)
                    self._json({"deleted": True})
                elif action == "reveal":
                    self._json({"api_key": repository.reveal(identifier, revision)})
                else:
                    self._json({"profile": repository.update(identifier, body.get("profile"), revision, api_key=body.get("api_key"), verification_token=body.get("verification_token"))})
            elif path == "/api/projects":
                if set(body) - {"path", "name"}:
                    raise AppError("打开项目的参数无效。", "invalid_project")
                self._json({"project": self.server.manager.open_project(body.get("path"), body.get("name"))}, 201)
            elif path in {"/api/projects/pick-folder", "/api/projects/pick-scene-file"}:
                if body:
                    raise AppError("文件选择窗口不接受目录或执行参数。")
                self._json(pick_native_path("folder" if path.endswith("pick-folder") else "scene"))
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/(current-scene|import-scene)", path):
                if match[2] == "current-scene":
                    self._json({"project": self.server.manager.select_scene(match[1], body)})
                else:
                    self._json(self.server.manager.import_scene(match[1], body), 202)
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/workspace/(checkpoint|undo|restore-batch|restore-checkpoint|settings)", path):
                self._json(self.server.manager.workspace_action(match[1], match[2], body))
            elif match := re.fullmatch(r"/api/projects/([0-9a-f]{32})/walkthrough", path):
                self._json(self.server.manager.open_walkthrough(match[1], body))
            elif path == "/api/runs":
                self._json(self.server.manager.create(body), 202)
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/resource-request/(approve|reject|resume)", path):
                from .agent_resources import HUMAN
                self._json(self.server.manager.resource_action(match[1], match[2], body, source=HUMAN), 202)
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/asset-request/(approve|resume|resource-continue)", path):
                service = getattr(self.server.manager,'asset_service',None)
                if service is None:
                    raise AppError('资产宿主不可用。','asset_trusted_host_unavailable',503)
                if match[2] == 'approve':
                    self._json({'asset_request':service.approve(match[1],body)},202)
                elif match[2] == 'resource-continue':
                    from .local_resource_warning import HUMAN
                    self._json({'asset_request':service.resource_continue(match[1],body,source=HUMAN)},202)
                else:
                    if set(body) != {'request_id','resume_id'}:
                        raise AppError('续做参数无效。','invalid_asset_resume')
                    doc = self.server.manager._asset_host().store(self.server.manager,match[1]).request(match[1])
                    if not doc or doc['id'] != body['request_id']:
                        raise AppError('资产请求身份不匹配。','asset_request_binding_conflict',409)
                    self._json(self.server.manager._asset_host().schedule_builder(self.server.manager,match[1],body['resume_id']),202)
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/director/(recover|resume|policy)", path):
                self._json(self.server.manager.director_action(match[1],match[2],body),202)
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/resume", path):
                self._json(self.server.manager.resume_local_round(match[1], body), 202)
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/local-round/end", path):
                self._json(self.server.manager.end_local_round(match[1], body))
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/(adopt|cancel)", path):
                action = self.server.manager.adopt if match[2] == "adopt" else self.server.manager.cancel
                self._json(action(match[1]), 202)
            else:
                raise AppError("接口不存在。", "not_found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(error)


def main():
    parser = argparse.ArgumentParser(description="SceneLoop 本地工作台和任务服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=PROJECT / ".sceneloop" / "runs")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 需要在 1–65535 之间。")
    server = None
    try:
        # Recovery rewrites active jobs. Establish directory ownership AND
        # reserve the endpoint first, including conflicts with older servers.
        acquire_process_lock(args.data_dir)
        server = LocalServer(("127.0.0.1", args.port), None)
        ensure_env(PROJECT)
        load_env(PROJECT / ".env")
        repository = ProfileRepository(args.data_dir / "_configuration")
        server.profile_repository = repository
        projects = ProjectRepository(args.data_dir / "_projects")
        server.manager = RunManager(args.data_dir, profile_repository=repository, project_repository=projects)
        from .asset_service import install
        install(server.manager).start()
        print(f"SceneLoop: http://localhost:{args.port}/ (Ctrl+C 停止)", flush=True)
        print("不会自动调用模型。请在工作台选择配置并启动任务。", flush=True)
        server.serve_forever(poll_interval=0.3)
    except (DataDirectoryInUse, ProfileError, CredentialError, ProjectError) as error:
        print(f"SceneLoop 未启动：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except OSError:
        print("SceneLoop 服务无法启动或继续运行：请检查目录权限与本地端口占用；不会停止已有服务。", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            if server.manager is not None and getattr(server.manager,'asset_service',None):
                server.manager.asset_service.stop()
            server.server_close()


if __name__ == "__main__":
    main()
