"""Trusted task-local resource grants. Not an HTTP/model authorization endpoint."""
from dataclasses import asdict
import json
import uuid
from .asset_provider import AssetJobError, HumanGrant, canonical_json, digest, identifier


def confirmed_local_resource_job(state, doc, job):
    """A completed local process has resource usage, not supplier credit cost.

    This never changes the generic cost fields or grants new authority. Unknown
    launch/read/write states and all cloud jobs still fail this predicate.
    """
    from .asset_run_store import AssetRunStore
    from .asset_jobs import controlled_path, file_facts
    try:
        profile=job['semantic']['profile'];cap=AssetRunStore(state).capability(doc['run_id'])
        if (profile.get('provider_id')!='hunyuan' or profile.get('mode')!='local_process'
                or profile.get('credential_mode')!='none' or profile.get('base_url')
                or job['dispatch']!='accepted' or job['remote']!='succeeded' or job['preparation']!='ready'
                or job['safe_error'] is not None or job['creation_attempts']!=1
                or job['cost']!='unknown' or job['credits'] is not None
                or job['task_id']!=job['job_id']
                or not cap or cap.get('local_authorization')!=doc.get('local_authorization')
                or cap['profile']!=doc['request']['profile'] or doc['job_id']!=job['job_id']
                or any(doc[k]!=job[k] for k in ('project_id','profile_digest','semantic_digest'))): return False
        authority=LocalAssetAuthority(state)
        auth=authority.read(**doc['local_authorization'],allow_stopped=True)
        if auth['profile_digest']!=job['profile_digest'] or authority.resolve(doc['grant_id']).snapshot()!=job['generation_grant']:
            return False
        raw=job['raw_artifact']
        _,facts=file_facts(controlled_path(state.project_root,raw['path']),32*1024*1024)
        return all(raw[k]==v for k,v in facts.items())
    except (AssetJobError,KeyError,TypeError,ValueError,OSError):
        return False


def confirmed_local_resource_failure(state, doc, job, *, open_wait=False, require_exit=False):
    """Known local resource consumption, not reconciliation of generic cost.

    Requires the original closed wait, SQL grant and bound launch/terminal files.
    Reads only: no Provider call, process adoption, mutation or missing-file repair.
    """
    from .asset_run_store import AssetRunStore
    from .asset_jobs import controlled_path
    from .hunyuan_process import read_json, same_process
    try:
        profile=job['semantic']['profile'];cap=AssetRunStore(state).capability(doc['run_id'])
        if (profile.get('provider_id')!='hunyuan' or profile.get('mode')!='local_process'
                or profile.get('credential_mode')!='none' or profile.get('base_url')
                or job['dispatch']!='accepted' or job['remote']!='failed' or job['preparation']!='not_started'
                or job['creation_attempts']!=1 or job['cost']!='unknown' or job['credits'] is not None
                or job['task_id']!=job['job_id'] or not (doc['closed'] or open_wait)
                or not cap or cap.get('local_authorization')!=doc.get('local_authorization')
                or cap['profile']!=doc['request']['profile'] or doc['job_id']!=job['job_id']
                or any(doc[k]!=job[k] for k in ('project_id','profile_digest','semantic_digest'))):return False
        authority=LocalAssetAuthority(state)
        auth=authority.read(**doc['local_authorization'],allow_stopped=True)
        if auth['profile_digest']!=job['profile_digest'] or authority.resolve(doc['grant_id']).snapshot()!=job['generation_grant']:return False
        directory=controlled_path(state.project_root,f".sceneloop/asset-jobs/{job['job_id']}/local")
        manifest,launch,terminal=[read_json(controlled_path(state.project_root,(directory/name).relative_to(state.project_root).as_posix()))
                                  for name in ('manifest.json','launched.json','terminal.json')]
        expected={'project_id':job['project_id'],'job_id':job['job_id'],'semantic_digest':job['semantic_digest'],'wire_digest':job['final_wire_digest']}
        if any(manifest['identity'].get(k)!=v for k,v in expected.items()) or manifest['identity'].get('stage')!='create':return False
        if any(record.get('nonce')!=manifest['nonce'] or record.get('manifest_digest')!=digest(manifest) for record in (launch,terminal)):return False
        if terminal.get('status')!='failed' or same_process(launch['process'],manifest['host_process']):return False
        if require_exit:
            from .hunyuan_process import process_identity
            from .hunyuan_provider import HunyuanProvider
            # The launcher and actual runner can be different Windows processes.
            # A terminal file alone does not prove either has stopped writing.
            running=HunyuanProvider(state.project_root)._runner(directory,manifest,launch)
            for expected in (launch['process'],running['process']):
                actual=process_identity(expected['pid'])
                if same_process(actual,expected) and actual['alive']:return False
        return True
    except (AssetJobError,KeyError,TypeError,ValueError,OSError):
        return False


class LocalAssetAuthority:
    def __init__(self, project):
        self.project = project

    def _has_tables(self):
        with self.project._connection() as connection:
            return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_local_authorizations'").fetchone())

    def _ensure_tables(self):
        # Schema creation belongs to the protected authorization write, never GET.
        with self.project._write() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS asset_local_authorizations "
                               "(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, document TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS asset_local_grants "
                               "(request_id TEXT PRIMARY KEY, grant_id TEXT UNIQUE NOT NULL, authorization_id TEXT NOT NULL, document TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS asset_local_revocations (id TEXT PRIMARY KEY, source TEXT NOT NULL)")

    def authorize(self, task_id, profile, limits, human_source):
        identifier(task_id)
        if (human_source not in {"protected_human_showcase", "protected_human_creation"}
                or profile.get("provider_id") != "hunyuan" or profile.get("mode") != "local_process"
                or not isinstance(limits, dict)
                or set(limits) != {"authorization_id", "max_generations", "job_timeout_seconds"}
                or type(limits["max_generations"]) is not int or not 0 <= limits["max_generations"] <= 12
                or type(limits["job_timeout_seconds"]) is not int or not 60 <= limits["job_timeout_seconds"] <= 3600):
            raise AssetJobError("asset_local_authorization_invalid")
        identifier(limits["authorization_id"])
        from .config_store import binding_fields
        fixed_profile = {key:profile[key] for key in ("id","revision",*binding_fields(profile))}
        document = {"project_id": self.project.project_id, "task_id": task_id,
                    "profile_digest": digest(fixed_profile), "source": human_source, **limits}
        self._ensure_tables()
        with self.project._write() as connection:
            prior = connection.execute("SELECT document FROM asset_local_authorizations WHERE id=?",
                                       (limits["authorization_id"],)).fetchone()
            if prior and json.loads(prior[0]) != document:
                raise AssetJobError("asset_local_authorization_conflict")
            connection.execute("INSERT OR IGNORE INTO asset_local_authorizations VALUES (?,?,?)",
                               (limits["authorization_id"], task_id, canonical_json(document)))
        return document

    def read(self, task_id, authorization_id, *, allow_stopped=False):
        if not self._has_tables():
            raise AssetJobError('asset_local_authorization_required')
        with self.project._connection() as connection:
            row = connection.execute("SELECT document FROM asset_local_authorizations WHERE id=? AND task_id=?",
                                     (authorization_id, task_id)).fetchone()
            used = connection.execute("SELECT COUNT(*) FROM asset_local_grants g JOIN asset_local_authorizations a ON a.id=g.authorization_id WHERE a.task_id=?",
                                      (task_id,)).fetchone()[0]
            revoked = connection.execute("SELECT 1 FROM asset_local_revocations WHERE id=?",(authorization_id,)).fetchone()
            original = connection.execute('SELECT document FROM asset_local_authorizations WHERE task_id=? ORDER BY rowid LIMIT 1', (task_id,)).fetchone()
        if not row:
            raise AssetJobError("asset_local_authorization_required")
        if revoked and not allow_stopped:
            raise AssetJobError("asset_local_authorization_stopped")
        document = json.loads(row[0])
        from .agent_resources import task_limit
        effective = task_limit(self.project, task_id, 'local_generations', json.loads(original[0])['max_generations'])
        return {**document, "effective_max_generations": effective, "used_generations": used, "stopped": bool(revoked)}

    def stop(self, task_id, authorization_id, human_source):
        if human_source not in {"protected_human_showcase", "protected_human_creation"}:
            raise AssetJobError("asset_local_authorization_invalid")
        if not self._has_tables():
            raise AssetJobError('asset_local_authorization_required')
        with self.project._write() as connection:
            if not connection.execute("SELECT 1 FROM asset_local_authorizations WHERE id=? AND task_id=?",
                                      (authorization_id,task_id)).fetchone():
                raise AssetJobError("asset_local_authorization_required")
            connection.execute("INSERT OR IGNORE INTO asset_local_revocations VALUES (?,?)",
                               (authorization_id,human_source))

    def claim(self, binding, request_id, snapshot):
        identifier(request_id)
        if not isinstance(binding, dict) or set(binding) != {"task_id", "authorization_id"}:
            raise AssetJobError("asset_local_authorization_required")
        if not self._has_tables():
            raise AssetJobError('asset_local_authorization_required')
        with self.project._write() as connection:
            row = connection.execute("SELECT document FROM asset_local_authorizations WHERE id=? AND task_id=?",
                                     (binding["authorization_id"], binding["task_id"])).fetchone()
            if not row:
                raise AssetJobError("asset_local_authorization_required")
            if connection.execute("SELECT 1 FROM asset_local_revocations WHERE id=?",
                                  (binding["authorization_id"],)).fetchone():
                raise AssetJobError("asset_local_authorization_stopped")
            auth = json.loads(row[0])
            if (auth["project_id"] != self.project.project_id or auth["profile_digest"] != snapshot["profile_digest"]
                    or snapshot["semantic"]["profile"]["provider_id"] != "hunyuan"
                    or snapshot["semantic"]["profile"]["mode"] != "local_process"):
                raise AssetJobError("asset_local_authorization_conflict")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_run_requests'").fetchone():
                original = connection.execute('SELECT document FROM asset_run_requests WHERE request_id=?',(request_id,)).fetchone()
                if original:
                    recorded = json.loads(original[0])
                    if recorded['closed']:
                        raise AssetJobError('asset_wait_ended')
                    if any(recorded.get(k) != snapshot.get(k) for k in ('run_id','stage_permission','reference','builder_mode')):
                        raise AssetJobError('asset_permission_identity_conflict')
            if snapshot.get('stage_permission'):
                from .asset_run_store import AssetRunStore
                from .local_asset_permission import check
                cap = AssetRunStore.capability_in(connection, snapshot['run_id'])
                check(connection, self.project, snapshot['run_id'], snapshot['stage_permission'], cap, reference=snapshot['reference'])
                if snapshot.get('previous_attempt'):
                    from .local_asset_failure import approved_retry, retry_identity
                    old=AssetRunStore(self.project).request(request_id=snapshot['previous_attempt']['id'],connection=connection)
                    approval=approved_retry(self.project,old,connection=connection)
                    if not approval or snapshot.get('retry_approval_id')!=approval['id'] or snapshot['id']!=retry_identity(old,approval):
                        raise AssetJobError('asset_local_retry_approval_required')
            elif snapshot.get('builder_mode'):
                raise AssetJobError('asset_stage_permission_required')
            grant = HumanGrant(uuid.uuid5(uuid.NAMESPACE_URL, digest({"authorization": auth, "request": request_id})).hex,
                self.project.project_id, snapshot["semantic_digest"], snapshot["profile_digest"], "generation",
                auth["source"] + ":" + auth["authorization_id"], "local resource authorization; no cloud fee authority")
            prior = connection.execute("SELECT document FROM asset_local_grants WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if json.loads(prior[0]) != asdict(grant):
                    raise AssetJobError("asset_local_grant_conflict")
                return grant
            used = connection.execute("SELECT COUNT(*) FROM asset_local_grants g JOIN asset_local_authorizations a ON a.id=g.authorization_id WHERE a.task_id=?",
                                      (auth['task_id'],)).fetchone()[0]
            # Original authorization/profile digest is never rewritten. A new
            # authorization ID cannot reset task consumption or raise its base.
            original = json.loads(connection.execute('SELECT document FROM asset_local_authorizations WHERE task_id=? ORDER BY rowid LIMIT 1', (auth['task_id'],)).fetchone()[0])
            from .agent_resources import task_limit
            limit = task_limit(self.project, auth['task_id'], 'local_generations', original['max_generations'], connection=connection)
            if used >= limit:
                raise AssetJobError("asset_local_generation_limit")
            connection.execute("INSERT INTO asset_local_grants VALUES (?,?,?,?)",
                               (request_id, grant.id, auth["authorization_id"], canonical_json(asdict(grant))))
        return grant

    def resolve(self, reference):
        if not self._has_tables():
            raise AssetJobError('asset_approval_unavailable')
        with self.project._connection() as connection:
            row = connection.execute("SELECT document FROM asset_local_grants WHERE grant_id=?", (reference,)).fetchone()
        if not row:
            raise AssetJobError("asset_approval_unavailable")
        return HumanGrant(**json.loads(row[0]))
