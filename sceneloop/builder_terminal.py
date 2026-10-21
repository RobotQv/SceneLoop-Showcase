"""Prove terminal Builder accounting and scope before handing off residual credits.

This is a read-only proof. It never settles an unknown operation, resumes its
worker, changes a pause, or supplies missing consumption from a default budget.
"""
import json
import threading

from .director_contract import digest


def excluded_assets(runtime, record, *, connection=None):
    """Frozen deny-scope; it can only propagate, never give a fresh cloud slot."""
    store = runtime.store(runtime.manager._get(record['parent_id'])['project_id'])
    if connection is None:
        with store.state._connection() as c:
            return excluded_assets(runtime, record, connection=c)
    blocked, excluded = _asset_scope(runtime, store, connection, record)
    if blocked:
        from .director_contract import reject
        reject('资产交接范围尚未确认：'+blocked['reason'], 'resource_source_unavailable', 409)
    return excluded


def _asset_scope(runtime, store, c, record):
    from .asset_run_store import AssetRunStore
    from .asset_provider import AssetJobError
    from .director_goal_continuity import _asset_settled
    rid = record['child_id']
    def fail(reason, identity):
        return {'scope':'asset','reason':reason,'run_id':rid,'claim_id':identity}, []
    row = c.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (rid,)).fetchone()
    inherited = ((json.loads(row[0]).get('execution_source') or {}).get('excluded_asset_jobs') or []) if row else []
    identities = {x['request_id']: x for x in inherited}
    own = [r[0] for r in c.execute('SELECT request_id FROM asset_run_requests WHERE run_id=?', (rid,))]
    excluded = []
    for request_id in sorted(set(own) | set(identities)):
        try:
            request = AssetRunStore(store.state).request(request_id=request_id, connection=c)
        except AssetJobError:
            return fail('asset_identity_unverified', request_id)
        if not request or not request['closed']:
            return fail('asset_request_open', request_id)
        if not request['job_id']:
            if request_id in identities:return fail('asset_identity_unverified', request_id)
            continue
        job = json.loads(c.execute('SELECT document FROM asset_generation_jobs WHERE job_id=?', (request['job_id'],)).fetchone()[0])
        identity = {'request_id':request_id,'run_id':request['run_id'],'job_id':request['job_id'],
            'submission_key':request['submission_key'],'grant_id':request['grant_id']}
        if request_id in identities and identities[request_id] != identity:
            return fail('asset_identity_unverified', request_id)
        if request_id not in identities and _asset_settled(store, c, job):
            continue
        # A closed, never-imported cloud job has no scene write scope. Keep
        # its unknown remote/fee state and slot, deny it in every successor.
        owner = runtime.manager._get(request['run_id'])
        binding = c.execute('SELECT document FROM director_delegations WHERE child_id=?', (request['run_id'],)).fetchone()
        worker = runtime.manager.workers.get(request['run_id'])
        continuation = c.execute('SELECT state FROM asset_run_resumes WHERE request_id=?', (request_id,)).fetchone()
        if (job['semantic']['profile']['provider_id'] == 'hunyuan'
                or request.get('import_intent') or request.get('import_receipt') or request.get('import_history')
                or continuation and (continuation[0] != 'started' or request.get('builder_completion') != 'completed')
                or owner.get('status') not in {'completed','failed','cancelled','interrupted'}
                or worker and worker.is_alive() and worker is not threading.current_thread()
                or not binding or json.loads(binding[0])['task_id'] != record['task_id']):
            return fail('asset_job_unresolved', request['job_id'])
        excluded.append(identity)
    return None, excluded


def blocker(runtime, record, *, finishing=False):
    store = runtime.store(runtime.manager._get(record['parent_id'])['project_id'])
    with store.state._connection() as c:
        return _check(runtime, store, c, record, finishing=finishing)


def _check(runtime, store, c, record, *, finishing=False):
    rid = record['child_id']
    def stop(scope, reason, identity=None):
        return {'scope': scope, 'reason': reason, 'run_id': rid, 'claim_id': identity}
    try:
        row = c.execute('SELECT document FROM director_delegations WHERE child_id=?', (rid,)).fetchone()
        bound = json.loads(row[0]) if row else {}
        run = runtime.manager._get(rid)
        parent = runtime.manager._get(record['parent_id'])
        result = bound.get('result') or {}
        proof = {k: v for k, v in result.items() if k != 'digest'}
        if (bound.get('state') != 'completed' or not result or digest(proof) != result.get('digest')
                or any(bound.get(k) != record.get(k) for k in ('parent_id', 'child_id', 'task_id', 'delegation_id'))
                or bound['task_id'] != parent.get('director_task_id')
                or run.get('project_id') != store.state.project_id
                or any(result.get(k) != v for k, v in {'task_id': bound['task_id'],
                    'delegation_id': bound['delegation_id'], 'child_run_id': rid,
                    'status': run.get('status'), 'usage': run.get('usage'), 'error':run.get('error')}.items())
                or result.get('result_id') != digest({'delegation': bound['delegation_id'], 'child': rid})):
            return stop('identity', 'terminal_result_unverified')
        if run.get('status') not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            return stop('worker', 'builder_not_terminal')
        worker = runtime.manager.workers.get(rid)
        if worker and worker.is_alive() and not (finishing and worker is threading.current_thread()):
            return stop('worker', 'builder_still_running')
        if run.get('director_ledger_unknown'):
            return stop('model_accounting', 'ledger_write_unknown')
        attempts = [json.loads(r[0]) for r in c.execute('SELECT document FROM director_attempts WHERE task_id=?', (bound['task_id'],))]
        attempts = [a for a in attempts if a['run_id'] == rid]
        for attempt in attempts:
            if (attempt.get('state') != 'settled' or attempt.get('cost_conflict') or attempt.get('cost_anomaly')
                    or (attempt.get('settlement') or {}).get('outcome') != 'returned'):
                return stop('model_accounting', 'model_attempt_unresolved', attempt['attempt_id'])
        usage = run.get('usage') or {}
        for role, field in (('builder', 'brain_calls'), ('judge', 'judge_calls')):
            count = usage.get(field)
            indices = [a.get('call_index') for a in attempts if a.get('role') == role]
            if type(count) is not int or count < 0 or sorted(indices) != list(range(1, count + 1)):
                return stop('resources', 'model_count_unverified', field)
        operations = list(c.execute('SELECT * FROM operations WHERE run_id=?', (rid,)))
        sql_attempts = list(c.execute('SELECT a.* FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=?', (rid,)))
        receipts = list(c.execute('SELECT * FROM receipts WHERE run_id=?', (rid,)))
        zero = (not attempts and not sql_attempts and not receipts and not operations
            and not (run.get('agent') or {}).get('observations')
            and not any(a.get('kind') in {'render','image','observation'} for a in run.get('artifacts', [])))
        used = (run.get('agent') or {}).get('used') or {}
        for field in ('tool_calls', 'observations'):
            value = used.get(field)
            if value is None and zero:
                continue
            if type(value) is not int or value < 0:
                return stop('resources', 'tool_usage_unverified', field)
        counter = c.execute('SELECT * FROM run_counters WHERE run_id=?', (rid,)).fetchone()
        committed = sum(r['kind'] == 'builder' for r in receipts)
        if (counter and (counter['needs_reconciliation'] or type(counter['saved_edit_rounds']) is not int
                         or counter['saved_edit_rounds'] != committed) or not counter and committed):
            return stop('working', 'save_count_unverified')
        for item in c.execute('SELECT key,detail FROM reconciliation'):
            detail = json.loads(item['detail'])
            # Migration/head uncertainty is a Working concern; unrelated Run
            # diagnostics are not silently promoted to a project-wide lock.
            if item['key'] != 'migration_backup' and (not isinstance(detail, dict)
                    or not detail.get('run_id') or detail.get('run_id') == rid):
                return stop('working', 'working_reconciliation', item['key'])
        rounds = [json.loads(r[0]) for r in c.execute('SELECT document FROM local_rounds WHERE run_id=?', (rid,))]
        for document in rounds:
            if document.get('status') not in {'ended', 'committed'}:
                return stop('candidate', 'local_round_not_closed', document.get('id'))
            checkpoint = (document.get('agent_state') or {}).get('used') or {}
            if any(type(checkpoint.get(k)) is int and checkpoint[k] > used.get(k, -1) for k in ('tool_calls', 'observations')):
                return stop('resources', 'tool_checkpoint_ahead')
        for operation in operations:
            if operation['state'] == 'committed':
                continue
            if not store.historical_candidate(c, operation, parent['id'], allow_superseded_initial=True,
                    allow_same_working=True, finishing_run=rid if finishing and worker is threading.current_thread() else None):
                scope = 'working' if operation['kind'] in {'initial', 'builder'} else 'candidate'
                return stop(scope, 'operation_unresolved', operation['operation_id'])
        asset_blocker, _ = _asset_scope(runtime, store, c, record)
        if asset_blocker:return asset_blocker
        from .agent_resources import project
        budget = project(run, store.state, connection=c)
        if not budget:
            return stop('resources', 'frozen_base_missing')
        for resource in budget['resources']:
            if (type(resource['used']) is not int or resource['used'] < 0
                    or resource['used'] > resource['effective_limit']):
                return stop('resources', 'resource_usage_unverified', resource['resource'])
        return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return stop('identity', 'terminal_evidence_incomplete')
