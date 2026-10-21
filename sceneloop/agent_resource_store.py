"""Append-only resource authority in the existing project SQLite database.

Reads never create a schema or upgrade historical Runs. Human action receipts,
grants and the original resume intent commit in one SQLite transaction.
"""
import copy
import json
import re
import uuid

from .director_contract import digest, reject
from .project_state_store import _now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ResourceStore:
    def __init__(self, project):
        self.project = project

    def exists(self):
        with self.project._connection() as c:
            return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_resource_bases'").fetchone())

    def ensure(self):
        with self.project._write() as c:
            for statement in (
                'CREATE TABLE IF NOT EXISTS agent_resource_bases (run_id TEXT PRIMARY KEY, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS agent_resource_checkpoints (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS agent_resource_requests (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS agent_resource_grants (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL, delta INTEGER NOT NULL, UNIQUE(request_id,resource,scope))',
                'CREATE TABLE IF NOT EXISTS agent_resource_actions (action_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, request_id TEXT NOT NULL)'):
                c.execute(statement)

    def freeze(self, run_id, document):
        self.ensure()
        with self.project._write() as c:
            row = c.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (run_id,)).fetchone()
            if row and json.loads(row[0]) != document:
                reject('资源基础快照身份冲突。', 'resource_base_conflict', 409)
            c.execute('INSERT OR IGNORE INTO agent_resource_bases VALUES (?,?)', (run_id, encoded(document)))

    def base(self, run_id):
        if not self.exists(): return None
        with self.project._connection() as c:
            row = c.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def grants(self, run_id, task_id, *, connection=None):
        if connection is None:
            if not self.exists(): return []
            with self.project._connection() as c:
                return self.grants(run_id, task_id, connection=c)
        return [dict(row) for row in connection.execute(
            'SELECT * FROM agent_resource_grants WHERE (scope=\'run\' AND scope_id=?) OR (scope=\'task\' AND scope_id=?) ORDER BY rowid',
            (run_id, task_id))]

    def checkpoint(self, identity=None, *, run_id=None):
        if not self.exists(): return None
        with self.project._connection() as c:
            row = (c.execute('SELECT document FROM agent_resource_checkpoints WHERE id=?', (identity,)).fetchone()
                   if identity else c.execute('SELECT document FROM agent_resource_checkpoints WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run_id,)).fetchone())
        return json.loads(row[0]) if row else None

    def save_checkpoint(self, document):
        self.ensure()
        with self.project._write() as c:
            prior = c.execute('SELECT document FROM agent_resource_checkpoints WHERE id=?', (document['id'],)).fetchone()
            if prior and json.loads(prior[0]) != document:
                reject('候选检查点身份冲突。', 'resource_checkpoint_conflict', 409)
            c.execute('INSERT OR IGNORE INTO agent_resource_checkpoints VALUES (?,?,?)',
                      (document['id'], document['run_id'], encoded(document)))

    def request(self, *, run_id=None, identity=None):
        if not self.exists(): return None
        with self.project._connection() as c:
            row = (c.execute('SELECT document FROM agent_resource_requests WHERE id=?', (identity,)).fetchone()
                   if identity else c.execute('SELECT document FROM agent_resource_requests WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run_id,)).fetchone())
        return json.loads(row[0]) if row else None

    @staticmethod
    def public(document):
        return {k: copy.deepcopy(v) for k, v in document.items() if not k.startswith('_')} if document else None

    def create_request(self, document):
        # The checkpoint/native envelope must already be durable before this call.
        with self.project._write() as c:
            row = c.execute('SELECT document FROM agent_resource_requests WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (document['run_id'],)).fetchone()
            if row:
                prior = json.loads(row[0])
                if prior['status'] == 'pending' or prior['continuation']['state'] in {'ready', 'running'}:
                    reject('当前资源申请尚未结束。', 'resource_request_pending', 409)
            cp = c.execute('SELECT document FROM agent_resource_checkpoints WHERE id=? AND run_id=?',
                           (document['checkpoint']['id'], document['run_id'])).fetchone()
            if not cp: reject('候选检查点未持久化。', 'resource_checkpoint_missing', 409)
            c.execute('INSERT INTO agent_resource_requests VALUES (?,?,?)', (document['id'], document['run_id'], encoded(document)))
        return document

    def action(self, identity, action, body, *, validate_only=False):
        required = {'request_id', 'expected_version', 'action_id'} | ({'approved', 'basis'} if action == 'approve' else {'basis'} if action == 'reject' else set())
        if (not isinstance(body, dict) or set(body) != required or body['request_id'] != identity
                or type(body['expected_version']) is not int or body['expected_version'] < 1
                or not isinstance(body['action_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{12,128}', body['action_id'])
                or ('basis' in body and (not isinstance(body['basis'], str) or not 1 <= len(body['basis'].strip()) <= 2000))):
            reject('可信资源动作参数无效。', 'resource_action_invalid')
        normalized = copy.deepcopy(body)
        if 'basis' in normalized: normalized['basis'] = normalized['basis'].strip()
        if action == 'approve' and isinstance(normalized.get('approved'), list):
            normalized['approved'] = sorted(normalized['approved'], key=encoded)
        fingerprint = digest({'action': action, 'body': normalized})
        with self.project._write() as c:
            row = c.execute('SELECT document FROM agent_resource_requests WHERE id=?', (identity,)).fetchone()
            if not row: reject('资源申请不存在。', 'resource_request_missing', 404)
            doc = json.loads(row[0])
            prior = c.execute('SELECT request_digest,request_id FROM agent_resource_actions WHERE action_id=?', (body['action_id'],)).fetchone()
            if prior:
                if prior[0] != fingerprint or prior[1] != identity:
                    reject('动作身份已用于不同内容。', 'resource_action_conflict', 409)
                return doc, False
            if doc['version'] != body['expected_version']:
                reject('资源申请版本已变。', 'resource_version_conflict', 409)
            if action == 'approve':
                if doc['status'] != 'pending': reject('申请已不待批准。', 'resource_request_state', 409)
                approved = body['approved']
                expected = [{'resource': r['resource'], 'scope': r['scope'], 'delta': r['requested_delta']} for r in doc['resources']]
                if (not isinstance(approved, list) or any(not isinstance(r, dict) or set(r) != {'resource', 'scope', 'delta'} or type(r['delta']) is not int for r in approved)
                        or sorted(approved, key=encoded) != sorted(expected, key=encoded)):
                    reject('首版必须精确批准本申请的全部正整数明细。', 'resource_approval_invalid')
                if validate_only: return doc, True
                for item in approved:
                    c.execute('INSERT INTO agent_resource_grants VALUES (?,?,?,?,?,?)',
                        (uuid.uuid4().hex, identity, item['resource'], item['scope'], doc['run_id'] if item['scope'] == 'run' else doc['task_id'], item['delta']))
                doc['approval'] = {'id': uuid.uuid4().hex, 'action_id': body['action_id'], 'approved_at': _now(),
                                   'approved': copy.deepcopy(approved), 'basis': body['basis'].strip()}
                doc['status'] = 'approved'
                doc['continuation'] = {'state': 'ready', 'can_resume': True, 'blocker': None}
                doc['_intent'] = {'id': uuid.uuid4().hex, 'state': 'prepared'}
            elif action == 'reject':
                if doc['status'] != 'pending': reject('申请已不待批准。', 'resource_request_state', 409)
                if validate_only: return doc, True
                doc['status'] = 'rejected'
                feedback = doc.get('kind') in {'local_asset_permission','creative_resources'}
                doc['continuation'] = {'state': 'ready' if feedback else 'cancelled', 'can_resume': feedback, 'blocker': None}
                if feedback:
                    doc['rejection'] = {'action_id':body['action_id'], 'basis':body['basis'].strip(), 'rejected_at':_now()}
                    doc['_intent'] = {'id': uuid.uuid4().hex, 'state': 'prepared'}
            elif action == 'resume':
                resumable = doc['status'] == 'approved' or doc.get('kind') in {'local_asset_permission','creative_resources'} and doc['status'] == 'rejected'
                if not resumable or doc.get('_intent', {}).get('state') != 'prepared':
                    reject('原恢复意图并非明确未启动。', 'resource_resume_blocked', 409)
                if validate_only: return doc, True
            else:
                reject('资源动作不存在。', 'resource_action_invalid')
            doc['version'] += 1
            doc['action_receipts'].append({'action_id': body['action_id'], 'action': action,
                'expected_version': body['expected_version'], 'resulting_version': doc['version'], 'status': 'accepted'})
            c.execute('INSERT INTO agent_resource_actions VALUES (?,?,?)', (body['action_id'], fingerprint, identity))
            c.execute('UPDATE agent_resource_requests SET document=? WHERE id=?', (encoded(doc), identity))
        return doc, True

    def transition(self, identity, state, *, blocker=None, expected_intent=None):
        with self.project._write() as c:
            row = c.execute('SELECT document FROM agent_resource_requests WHERE id=?', (identity,)).fetchone()
            if not row: reject('资源申请不存在。', 'resource_request_missing', 404)
            doc = json.loads(row[0])
            if expected_intent is not None and doc.get('_intent', {}).get('state') != expected_intent:
                return None
            if state == 'running' and not (doc['status'] == 'approved' or doc.get('kind') in {'local_asset_permission','creative_resources'} and doc['status'] == 'rejected'): return None
            if state == 'cancelled': doc['status'] = 'cancelled'
            if state == 'blocked': doc['status'] = 'blocked'
            doc['continuation'] = {'state': state, 'can_resume': False, 'blocker': blocker}
            if doc.get('_intent'): doc['_intent']['state'] = 'started' if state == 'running' else state
            doc['version'] += 1
            c.execute('UPDATE agent_resource_requests SET document=? WHERE id=?', (encoded(doc), identity))
        return doc
