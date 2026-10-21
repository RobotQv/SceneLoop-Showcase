"""Only the two asset tables are writable here; reuse project short transactions."""
from __future__ import annotations

import json
import sqlite3

from .asset_provider import AssetJobError, canonical_json, digest, validate_job
from .project_state_store import StateStoreError


MAX_DOCUMENT_BYTES = 512 * 1024
MAX_EVENTS = 4096
ACTIONS = {"prepare", "resume", "stop", "stopped", "upload_gate", "upload_reply", "upload_unknown",
           "renew_upload", "token_invalid", "create_gate", "create_reply", "create_unknown",
           "reconcile_upload", "reconcile_create", "query_gate", "query_reply", "query_unknown",
           "cancel_reply", "cancel_unknown", "accounting", "recover", "preparation_gate", "raw_bound",
           "ready", "preparation_blocked", "preparation_failed", "recheck", "receipt_conflict"}


class AssetJobStore:
    def stop_local(self, job_id):
        """Only local SQL transitions, independent of provider or credentials."""
        for _ in range(8):
            job = self.read(job_id)
            if job['local'] == 'stopped':
                return job
            stopped = job['local'] == 'stop_requested'
            try:
                self.update(job_id,job['version'],{'local':'stopped' if stopped else 'stop_requested'},
                            action='stopped' if stopped else 'stop')
            except AssetJobError as error:
                if error.code != 'asset_job_version_conflict': raise
        raise AssetJobError('asset_job_version_conflict')

    def __init__(self, project_store):
        self.project = project_store
        # This does not initialize or import a project manifest.
        loaded = self.project.load()
        if loaded is None:
            raise AssetJobError("asset_project_not_initialized")

    def _decode(self, row):
        if row is None:
            raise AssetJobError("asset_job_not_found")
        try:
            document = json.loads(row["document"])
            validate_job(document)
            if (row["project_id"] != self.project.project_id or document["project_id"] != row["project_id"]
                    or row["job_id"] != document["job_id"] or row["version"] != document["version"]
                    or row["submission_key"] != document["submission_key"]
                    or row["semantic_digest"] != document["semantic_digest"]
                    or row["generation_authorization_id"] != document["generation_grant"]["id"]):
                raise AssetJobError("asset_job_state_integrity_error")
            return document
        except (ValueError, TypeError, KeyError):
            raise AssetJobError("asset_job_state_integrity_error") from None

    def read(self, job_id):
        with self.project._connection() as connection:
            return self._decode(connection.execute(
                "SELECT * FROM asset_generation_jobs WHERE job_id=? AND project_id=?",
                (job_id, self.project.project_id)).fetchone())

    def by_key(self, submission_key):
        with self.project._connection() as connection:
            row = connection.execute(
                "SELECT * FROM asset_generation_jobs WHERE project_id=? AND submission_key=?",
                (self.project.project_id, submission_key)).fetchone()
            return self._decode(row) if row else None

    @staticmethod
    def _serialized(document):
        encoded = canonical_json(document)
        if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise AssetJobError("asset_job_document_limit")
        return encoded

    @staticmethod
    def _event(connection, job, action, *, grant=None):
        if action not in ACTIONS or job["version"] > MAX_EVENTS:
            raise AssetJobError("asset_job_audit_limit")
        # No caller-supplied text, raw response, token, signed URL or grant contents.
        document = {"sequence": job["version"], "action": action,
                    **{key: job[key] for key in ("upload", "dispatch", "remote", "local", "preparation", "cost",
                                                "creation_attempts", "upload_attempts", "safe_error")}}
        connection.execute(
            "INSERT INTO asset_generation_events VALUES (?,?,?,?,?,?)",
            (job["job_id"], job["version"], job["project_id"], grant["scope"] if grant else None,
             grant["id"] if grant else None, canonical_json(document)))

    def create(self, job):
        validate_job(job)
        serialized = self._serialized(job)
        try:
            with self.project._write() as connection:
                existing = connection.execute(
                    "SELECT * FROM asset_generation_jobs WHERE project_id=? AND submission_key=?",
                    (self.project.project_id, job["submission_key"])).fetchone()
                if existing:
                    old = self._decode(existing)
                    if (old["semantic_digest"] != job["semantic_digest"]
                            or old["generation_grant"] != job["generation_grant"]):
                        raise AssetJobError("asset_submission_conflict")
                    return old, False
                if job["project_id"] != self.project.project_id or job["version"] != 1:
                    raise AssetJobError("asset_job_state_integrity_error")
                connection.execute("INSERT INTO asset_generation_jobs VALUES (?,?,?,?,?,?,?)",
                                   (job["project_id"], job["job_id"], job["submission_key"],
                                    job["generation_grant"]["id"], job["semantic_digest"], 1, serialized))
                self._event(connection, job, "prepare")
            self._after_commit("prepare", job)
            return job, True
        except StateStoreError as error:
            if isinstance(error.__cause__, sqlite3.IntegrityError):
                raise AssetJobError("asset_approval_already_bound") from None
            raise

    def update(self, job_id, expected_version, patch, *, action, consume_grant=None, bridge_gate=None):
        try:
            with self.project._write() as connection:
                old = self._decode(connection.execute(
                    "SELECT * FROM asset_generation_jobs WHERE job_id=? AND project_id=?",
                    (job_id, self.project.project_id)).fetchone())
                if old["version"] != expected_version:
                    raise AssetJobError("asset_job_version_conflict")
                job = {**old, **patch, "version": old["version"] + 1}
                validate_job(job, old, action=action)
                if action in {"upload_gate", "create_gate"}:
                    expected = job["upload_grant" if action == "upload_gate" else "generation_grant"]
                    if consume_grant != expected:
                        raise AssetJobError("asset_approval_invalid")
                    if old['semantic']['profile']['provider_id'] == 'hunyuan':
                        self._check_local_grant(connection, old)
                    linked = connection.execute('SELECT * FROM asset_run_requests WHERE grant_id=?',
                                                (old['generation_grant']['id'],)).fetchone()
                    if linked:
                        doc = json.loads(linked['document'])
                        from .asset_run_store import AssetRunStore
                        AssetRunStore.check_checkpoint(connection,doc)
                        if old['semantic']['profile']['provider_id'] == 'hunyuan':
                            binding = doc.get('local_authorization')
                            expected_gate = {'request_id':doc['id'],'request_version':doc['version'],
                                'job_version':old['version'],'local_authorization':binding,
                                'grant_digest':digest(doc.get('local_grant'))}
                            auth_row = connection.execute(
                                'SELECT document FROM asset_local_authorizations WHERE id=? AND task_id=?',
                                ((binding or {}).get('authorization_id'),(binding or {}).get('task_id'))).fetchone()
                            grant_row = connection.execute('SELECT document FROM asset_local_grants WHERE request_id=? AND grant_id=?',
                                (doc['id'],old['generation_grant']['id'])).fetchone()
                            stopped = connection.execute('SELECT 1 FROM asset_local_revocations WHERE id=?',
                                ((binding or {}).get('authorization_id'),)).fetchone()
                            auth = json.loads(auth_row[0]) if auth_row else {}
                            if (action != 'create_gate' or linked['closed'] or linked['job_id'] != job_id
                                    or bridge_gate != expected_gate or not auth_row or stopped or not grant_row
                                    or json.loads(grant_row[0]) != doc.get('local_grant')
                                    or auth.get('profile_digest') != job['profile_digest']
                                    or auth.get('project_id') != job['project_id']
                                    or doc['wire_digest'] != job['final_wire_digest']):
                                raise AssetJobError('asset_local_authorization_conflict')
                        else:
                            expected_gate = {'request_id':doc['id'],'request_version':doc['version'],
                                'job_version':old['version'],'price_digest':digest(doc['price_check'])}
                            if (action != 'create_gate' or linked['closed'] or linked['job_id'] != job_id
                                    or not doc['price_check'] or bridge_gate != expected_gate
                                    or doc['price_check']['wire_digest'] != job['final_wire_digest']):
                                raise AssetJobError('asset_bridge_price_gate_required')
                elif consume_grant is not None:
                    raise AssetJobError("asset_approval_invalid")
                serialized = self._serialized(job)
                changed = connection.execute(
                    "UPDATE asset_generation_jobs SET version=?,document=? WHERE job_id=? AND version=?",
                    (job["version"], serialized, job_id, expected_version)).rowcount
                if changed != 1:
                    raise AssetJobError("asset_job_version_conflict")
                self._event(connection, job, action, grant=consume_grant)
            self._after_commit(action, job)
            return job
        except StateStoreError as error:
            if isinstance(error.__cause__, sqlite3.IntegrityError):
                raise AssetJobError("asset_approval_consumed") from None
            raise

    @staticmethod
    def _check_local_grant(connection, job):
        # Even a direct coordinator cannot use a cloud or revoked local grant.
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_local_grants'").fetchone():
            raise AssetJobError('asset_local_authorization_required')
        row = connection.execute('SELECT g.document,a.document,r.id FROM asset_local_grants g '
            'JOIN asset_local_authorizations a ON a.id=g.authorization_id '
            'LEFT JOIN asset_local_revocations r ON r.id=a.id WHERE g.grant_id=?',
            (job['generation_grant']['id'],)).fetchone()
        if not row or row[2]:
            raise AssetJobError('asset_local_authorization_stopped' if row else 'asset_local_authorization_required')
        grant, auth = json.loads(row[0]), json.loads(row[1])
        if ({**grant,'digest':digest(grant)} != job['generation_grant']
                or auth['project_id'] != job['project_id'] or auth['profile_digest'] != job['profile_digest']):
            raise AssetJobError('asset_local_authorization_conflict')
        return auth

    def check_local_admission(self, job):
        with self.project._connection() as connection:
            return self._check_local_grant(connection, job)

    def events(self, job_id):
        self.read(job_id)
        with self.project._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT document FROM asset_generation_events WHERE job_id=? AND project_id=? ORDER BY sequence",
                (job_id, self.project.project_id))]

    def _after_commit(self, action, job):
        """Observation point; an exception here is a lost local commit ACK."""
