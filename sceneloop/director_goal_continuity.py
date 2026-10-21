"""Adopt a proven compatible-edit Goal at a new trusted construction boundary.

The caller owns the existing project write transaction. This changes only the
task's current reference and appends a consumed audit event; no history, scene,
authorization, resource ledger or progress guard is rewritten.
"""
import copy
import json
from datetime import datetime

from .director_contract import digest, reject
from .refinement_plan import selection_identity


def reference(goal):
    return {k: goal[k] for k in ('id', 'version', 'content_digest')} if goal else None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _asset_settled(store, c, job):
    # Legacy local jobs retain cost=unknown after a known process exit. Reuse
    # SQL grant + terminal-file proof, never just the provider label.
    from .asset_local_authorization import confirmed_local_resource_failure, confirmed_local_resource_job
    from .asset_run_store import AssetRunStore
    from .asset_provider import AssetJobError
    try:
        request = c.execute('SELECT request_id FROM asset_run_requests WHERE job_id=? AND closed=1',
            (job['job_id'],)).fetchone()
        doc = AssetRunStore(store.state).request(request_id=request[0], connection=c) if request else None
        if doc and (confirmed_local_resource_failure(store.state, doc, job)
                or confirmed_local_resource_job(store.state, doc, job)):
            return True
    except AssetJobError:
        return False
    return (job.get('remote') in {'succeeded', 'failed', 'cancelled', 'banned', 'expired'}
        and job.get('dispatch') not in {'submitting', 'outcome_unknown'}
        and job.get('cost') in {'consumed', 'released', 'no_charge_confirmed'})


def _quiet(store, c, parent, task):
    """Reuse existing historical-candidate proof for safely isolated old files."""
    rid = parent['id']
    for row in c.execute('SELECT document FROM director_delegations'):
        if json.loads(row[0])['state'] in {'reserved', 'prepared', 'started', 'waiting'}:
            return False
    for row in c.execute('SELECT document FROM director_attempts WHERE task_id=?', (task['task_id'],)):
        attempt = json.loads(row[0])
        if (attempt.get('cost_conflict') or attempt.get('cost_anomaly') or attempt['state'] == 'unknown'
                or attempt['state'] != 'settled' and attempt['run_id'] != rid):
            return False
    for row in c.execute('SELECT document FROM director_decisions'):
        decision = json.loads(row[0])
        if decision['state'] == 'unknown' or decision['state'] == 'started' and decision['parent_id'] != rid:
            return False
        if decision['state'] == 'returned' and decision['parent_id'] != rid:
            # Returned describes the model response, not completion of its
            # tools. A lost process after batch.started may have side effects.
            batch = decision.get('batch') or {}
            calls = (decision.get('assistant') or {}).get('tool_calls') or []
            results = batch.get('outcomes') or []
            if (batch.get('started') is not None or calls and
                    (batch.get('cursor') != len(calls) or len(results) != len(calls)
                     or any((result.get('tool') or {}).get('tool_call_id') != call['id']
                            for call, result in zip(calls, results)))):
                return False
    if (c.execute('SELECT 1 FROM reconciliation LIMIT 1').fetchone()
            or c.execute('SELECT 1 FROM run_counters WHERE needs_reconciliation!=0 LIMIT 1').fetchone()
            or c.execute("SELECT 1 FROM local_rounds WHERE status IN ('active','paused') LIMIT 1").fetchone()
            or c.execute('SELECT 1 FROM asset_run_requests WHERE closed=0 LIMIT 1').fetchone()):
        return False
    for operation in c.execute("SELECT * FROM operations WHERE state IN ('prepared','unknown')"):
        if not store.historical_candidate(c, operation, rid, allow_superseded_initial=True):
            return False
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'director_observe_claims' in tables:
        if any(json.loads(row[0]).get('state') != 'returned'
                for row in c.execute('SELECT document FROM director_observe_claims')):
            return False
    if 'asset_generation_jobs' in tables:
        for row in c.execute('SELECT document FROM asset_generation_jobs'):
            job = json.loads(row[0])
            if not _asset_settled(store, c, job):
                return False
    for run in store.run_states.values():
        if run.get('project_id') != store.state.project_id or run['id'] == rid:
            continue
        worker = store.worker_states.get(run['id'])
        if (run.get('director_ledger_unknown') or worker and worker.is_alive() and run.get('operation') != 'conversation'
                or run.get('operation') != 'conversation' and run.get('status') not in {'completed', 'failed', 'cancelled', 'interrupted'}):
            return False
    return True


def continue_goal(store, c, parent, task, goal, plan, project, *, error='director_stale'):
    """Fail closed unless SQL and accepted requests prove this exact transition."""
    current = reference(goal)
    if task.get('goal_ref') is None or task['goal_ref'] == current:
        return None
    def denied():
        reject('持续任务Goal与当前场景不一致；只有新的可信施工请求可接续已确认的兼容编辑。', error, 409)
    if store is None:
        denied()
    try:
        session_row = c.execute('SELECT document FROM director_sessions WHERE parent_id=?', (parent['id'],)).fetchone()
        association = c.execute('SELECT * FROM director_parents WHERE parent_id=?', (parent['id'],)).fetchone()
        session = json.loads(session_row[0]) if session_row else {}
        accepted = session.get('run', {})
        scope = session.get('request_scope', {})
        from .director_request_scope import managed_authorization
        if (not association or association['task_id'] != task['task_id']
                or parent.get('director_task_id') != task['task_id']
                or accepted.get('project_id') != store.state.project_id or parent.get('project_id') != store.state.project_id
                or accepted.get('request_fingerprint') != association['digest']
                or accepted.get('request_fingerprint') != parent.get('request_fingerprint')
                or accepted.get('request_protocol') != 'natural_v2'
                or scope.get('state') not in {'classified', 'active'}
                or not managed_authorization(accepted, scope, session=session, connection=c)
                or store.pause_blocks(task, accepted, scope, c=c)
                or parent.get('cancel_requested') or parent.get('director_ledger_unknown')
                or project['revision'] != parent['expected_project_revision']
                or c.execute('SELECT 1 FROM refinement_bindings WHERE run_id=?', (parent['id'],)).fetchone()
                or plan['goal_id'] != goal['id'] or plan['project_id'] != store.state.project_id
                or not _quiet(store, c, parent, task)):
            denied()
        old_row = c.execute('SELECT snapshot FROM refinement_goals WHERE goal_id=?', (task['goal_ref']['id'],)).fetchone()
        old = json.loads(old_row[0]) if old_row else None
        if not old or reference(old) != task['goal_ref'] or old['project_id'] != store.state.project_id:
            denied()
        evolved = [json.loads(r[0]) for r in c.execute(
            'SELECT snapshot FROM refinement_goals WHERE version>? ORDER BY version', (old['version'],))]
        if not evolved or reference(evolved[-1]) != current:
            denied()
        source_ids = []
        for entry in evolved:
            source = entry.get('source', {})
            source_id = source.get('run_id')
            run = store.run_states.get(source_id, {})
            bound = c.execute('SELECT binding FROM refinement_bindings WHERE run_id=?', (source_id,)).fetchone()
            binding = json.loads(bound[0]) if bound else {}
            if (source.get('kind') != 'execution_request' or entry['project_id'] != store.state.project_id
                    or run.get('project_id') != store.state.project_id or run.get('operation') != 'edit'
                    or run.get('status') != 'completed' or run.get('error') or run.get('director_delegation')
                    or c.execute('SELECT 1 FROM director_delegations WHERE child_id=?', (source_id,)).fetchone()
                    or c.execute('SELECT 1 FROM director_parents WHERE parent_id=?', (source_id,)).fetchone()
                    or reference(binding.get('goal')) != reference(entry) or binding.get('discussion')
                    or binding.get('project_id') != store.state.project_id or binding.get('request') != run.get('prompt')
                    or datetime.fromisoformat(accepted['created_at']) <= datetime.fromisoformat(entry['created_at'])):
                denied()
            receipts = [json.loads(r[0]) for r in c.execute(
                "SELECT r.receipt FROM receipts r JOIN operations o ON o.operation_id=r.operation_id "
                "WHERE r.run_id=? AND r.project_id=? AND r.kind='builder' AND o.state='committed'",
                (source_id, store.state.project_id))]
            if not receipts:
                denied()
            if entry['id'] == goal['id']:
                matching = [r for r in receipts if r['working_version'] == project['workspace']['working_version']
                    and selection_identity(r.get('target_selection')) == selection_identity(project.get('current_scene'))]
                if len(matching) != 1:
                    denied()
                receipt = matching[0]
                accepted_working = (accepted.get('readonly_accepted') or {}).get('working') or {}
                if (datetime.fromisoformat(accepted['created_at']) <= datetime.fromisoformat(receipt['committed_at'])
                        or accepted_working.get('working_version') != project['workspace']['working_version']
                        or selection_identity(accepted_working.get('selection')) != selection_identity(project.get('current_scene'))):
                    denied()
                mapping = c.execute('SELECT selection,artifacts,availability FROM scene_revisions WHERE revision_id=?',
                    (receipt['global_revision_id'],)).fetchone()
                if (not mapping or mapping['availability'] != 'available'
                        or selection_identity(json.loads(mapping['selection'])) != selection_identity(project.get('current_scene'))
                        or json.loads(mapping['artifacts']) != receipt['artifacts']):
                    denied()
            source_ids.append(source_id)
    except (KeyError, ValueError, TypeError, AttributeError):
        denied()
    event_id = digest({'kind': 'goal_continuity', 'parent_id': parent['id'], 'from': task['goal_ref'], 'to': current})
    receipt = {'kind': 'goal_continuity', 'source': 'trusted_construction_boundary',
        'parent_id': parent['id'], 'task_id': task['task_id'], 'from': copy.deepcopy(task['goal_ref']),
        'to': current, 'source_run_ids': source_ids, 'plan_id': plan['id'], 'plan_version': plan['version'],
        'working_version': project['workspace']['working_version'], 'reason': 'confirmed_compatible_edit',
        'state': 'applied', 'result': None}
    task['goal_ref'] = current
    task['revision'] = task.get('revision', 1) + 1
    c.execute('UPDATE director_tasks SET document=? WHERE slot=1', (encoded(task),))
    c.execute('INSERT INTO director_events VALUES (?,?,?,1)', (event_id, parent['id'], encoded(receipt)))
    return receipt


def stale_signature(store, parent, scope):
    """Equivalent stale tools share one guard, anchored to actual current facts."""
    with store.state._connection() as c:
        project = json.loads(store.state._read_row(c)['manifest'])
        goal = c.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()
        plan = c.execute('SELECT document FROM refinement_plans WHERE singleton=1').fetchone()
        task = json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        session = json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?', (parent['id'],)).fetchone()[0])
    return digest({'error_family': 'stale_goal_boundary', 'goal': reference(json.loads(goal[0])) if goal else None,
        'task_goal': task.get('goal_ref'), 'plan': json.loads(plan[0]) if plan else None,
        'working': project.get('current_scene'), 'working_version': project['workspace']['working_version'],
        'scope': scope, 'observations': sorted({r['identity'] for r in session.get('observations', []) if r.get('identity')})})
