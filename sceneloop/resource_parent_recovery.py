"""Prove a Director's resource-waiting child without starting any execution.

The same authority check protects parent- and child-addressed approvals. A
startup exception to interrupted requires settled SQL/native evidence. A narrow
human recovery can reconcile the old local-asset startup misclassification;
the original interruption event and error remain in its reconciliation record.
"""
import copy
import json
import sqlite3

from .director_contract import digest, reject
from .director_continuation import Continuation, public_assistant
from .providers import _agent_history


def _deny():
    reject('父任务等待身份或原调用状态不可确认；不能借子任务追加额度绕过。',
           'resource_parent_unverified', 409)


def _parent(manager, child, *, reconcile=False):
    delegated = child.get('director_delegation')
    if not delegated: return None
    state = manager._projects().state_store(child['project_id'])
    with state._connection() as c:
        required = {'director_parents', 'director_sessions', 'director_delegations', 'director_decisions',
                    'director_events', 'director_tasks', 'director_attempts', 'agent_resource_bases'}
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables: _deny()
        row = c.execute('SELECT * FROM director_delegations WHERE child_id=?', (child['id'],)).fetchone()
        if not row: _deny()
        binding = json.loads(row['document'])
        if any(binding.get(key) != row[key] for key in ('delegation_id', 'parent_id', 'call_id', 'child_id')): _deny()
        if any(binding.get(key) != delegated.get(key) for key in
               ('delegation_id', 'parent_id', 'call_id', 'child_id', 'task_id', 'brief', 'submission_digest')): _deny()
        completed = reconcile and binding['state'] == 'completed'
        if not completed and (binding['state'] not in {'started', 'waiting'} or binding.get('result')): _deny()
        parent = manager.runs.get(binding['parent_id'])
        if (not parent or parent.get('operation') != 'conversation'
                or parent.get('project_id') != child['project_id'] or not reconcile and parent.get('status') != 'waiting_child'
                or not reconcile and parent.get('active_child_id') != child['id'] or not parent.get('agent_resource_base')): _deny()
        if (parent.get('error') and not reconcile) or parent.get('cancel_requested') or parent.get('director_ledger_unknown'):
            reject('原父任务已取消或有未核实错误，不能通过子任务批准绕过。', 'resource_parent_unknown', 409)
        parent_row = c.execute('SELECT task_id FROM director_parents WHERE parent_id=?', (parent['id'],)).fetchone()
        session_row = c.execute('SELECT document FROM director_sessions WHERE parent_id=?', (parent['id'],)).fetchone()
        task_row = c.execute('SELECT document FROM director_tasks WHERE task_id=?', (binding['task_id'],)).fetchone()
        if (not parent_row or parent_row[0] != binding['task_id'] or not session_row or not task_row
                or parent.get('director_task_id') != binding['task_id']): _deny()
        task, session = json.loads(task_row[0]), json.loads(session_row[0])
        if task.get('pause'):
            from .director_request_scope import managed,pause_applies
            host=manager._director_host()
            if reconcile:
                if task['pause'].get('scope') == 'all_models': _deny()
            elif not managed(host,parent) or pause_applies(host,parent):_deny()
        if session.get('parent_id') != parent['id']: _deny()
        if not completed:
            if (session.get('state') != 'waiting_child' or session.get('child_id') != child['id']
                    or session.get('decision') or session.get('turn_pending')): _deny()
        else:
            result = binding.get('result') or {}
            if (session.get('state') not in {'waiting_child','result_prepared','reply_confirmed','completed'}
                    or result.get('child_run_id') != child['id'] or result.get('delegation_id') != binding['delegation_id']
                    or result.get('task_id') != binding['task_id'] or result.get('status') != child['status']
                    or result.get('digest') != digest({k:v for k,v in result.items() if k != 'digest'})): _deny()
        if any(parent.get(k) != session['run'].get(k) for k in
               ('id', 'project_id', 'profiles', 'execution', 'request_fingerprint', 'idempotency_key')): _deny()
        snapshot = binding.get('child_snapshot') or {}
        if any(child.get(k) != snapshot.get(k) for k in
               ('id', 'project_id', 'profiles', 'execution', 'iteration', 'judge', 'bootstrap',
                'request_fingerprint', 'agent_resource_base')): _deny()
        # There may be historical completed children, but exactly one writer
        # claim may still own this parent/project. Do not substitute a new child.
        bindings = [json.loads(r[0]) for r in c.execute('SELECT document FROM director_delegations WHERE parent_id=?', (parent['id'],))]
        active = [r for r in bindings if r.get('state') in {'reserved', 'prepared', 'started', 'waiting'}]
        if completed and active or not completed and (len(active) != 1 or active[0]['child_id'] != child['id']): _deny()
        children = [child]
        if reconcile:
            children = []
            for other in bindings:
                sibling = manager.runs.get(other['child_id'])
                if (not sibling or sibling.get('project_id') != child['project_id'] or sibling.get('cancel_requested')
                        or sibling.get('director_ledger_unknown') or (sibling.get('error') or {}).get('outcome_unknown')): _deny()
                if sibling['id'] != child['id'] or completed:
                    result = other.get('result') or {}
                    if (other['state'] != 'completed' or sibling['status'] not in {'completed','failed'}
                            or result.get('status') != sibling['status'] or result.get('child_run_id') != sibling['id']
                            or result.get('delegation_id') != other['delegation_id'] or result.get('task_id') != binding['task_id']
                            or result.get('digest') != digest({k:v for k,v in result.items() if k != 'digest'})): _deny()
                children.append(sibling)
            if completed and session.get('result') not in [r.get('result') for r in bindings]: _deny()
        decisions = [json.loads(r[0]) for r in c.execute(
            'SELECT d.document FROM director_decisions d JOIN director_events e ON e.event_id=d.event_id WHERE e.parent_id=?', (parent['id'],))]
        if not decisions or any(d.get('parent_id') != parent['id'] or d.get('state') != 'applied' for d in decisions): _deny()
        reference = session.get('private_ref') or {}
        decision = next((d for d in decisions if d['event_id'] == reference.get('event_id')), None)
        if (not decision or reference.get('slot') != 'exchange'
                or not completed and (decision.get('child_id') != child['id'] or session['event_id'] != decision['event_id'])): _deny()
        delegation = next((d for d in decisions if d.get('child_id') == child['id']), None)
        if not delegation: _deny()
        if completed:
            calls = delegation['assistant'].get('tool_calls', [])
            batch = delegation.get('batch') or {}
            if (batch.get('started') is not None or batch.get('cursor') != len(calls)
                    or len(batch.get('outcomes', [])) != len(calls)): _deny()
            for call, outcome in zip(calls, batch['outcomes']):
                if (outcome.get('tool') or {}).get('tool_call_id') != call['id']: _deny()
        attempts = [json.loads(r[0]) for r in c.execute('SELECT document FROM director_attempts WHERE task_id=?', (binding['task_id'],))]
        ids = {parent['id'],*(r['id'] for r in children)}
        relevant = [a for a in attempts if a.get('run_id') in ids]
        if any(a.get('state') != 'settled' or a.get('settlement', {}).get('outcome') != 'returned' or a.get('cost_conflict') for a in relevant): _deny()
        counts = [(parent['id'],'director',parent['usage']['director_calls'])]
        for sibling in children:
            counts.extend([(sibling['id'],'builder',sibling['usage']['brain_calls']),
                           (sibling['id'],'judge',sibling['usage'].get('judge_calls',0))])
        for rid, role, used in counts:
            if len([a for a in relevant if a['run_id'] == rid and a['role'] == role]) != used: _deny()
        for rid in ids:
            if (c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='unknown'", (rid,)).fetchone()
                    or c.execute("SELECT 1 FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')", (rid,)).fetchone()
                    or reconcile and c.execute('SELECT 1 FROM run_counters WHERE run_id=? AND needs_reconciliation!=0',(rid,)).fetchone()): _deny()
    # Validate the parent's original protected exchange, not a reconstructed
    # public transcript. Monetary unpriced is allowed when outcome is settled.
    assistant = Continuation(manager.run_directory(parent['id']), parent['project_id'], parent['id']).load(
        reference, decision, tools=session['exchange'][1:])
    if public_assistant(assistant) != session['exchange'][0]: _deny()
    exchange = [assistant, *session['exchange'][1:]]
    _agent_history(exchange, director_arguments=True)
    call = next((q for q in delegation['assistant'].get('tool_calls', []) if q['id'] == binding['call_id']), None)
    receipt = next((q for q in exchange[1:] if q.get('tool_call_id') == binding['call_id']), None)
    if completed:
        outcome = next((q for q in delegation['batch']['outcomes'] if q['tool']['tool_call_id'] == binding['call_id']), {})
        receipt = outcome.get('tool')
    if not call or call['function']['name'] != 'delegate_task' or not receipt: _deny()
    result = json.loads(receipt['content'])
    if (result.get('ok') is not True or result.get('result', {}).get('child_run_id') != child['id']
            or result['result'].get('delegation_id') != binding['delegation_id']
            or result['result'].get('task_id') != binding['task_id']): _deny()
    # Also validate the parent frozen base against SQL; no mutable Run JSON grant.
    from .agent_resources import project
    project(parent, state)
    return parent


def guard_parent(manager, child):
    """Approval and resumed execution cannot bypass a genuinely unknown parent."""
    try:
        return _parent(manager, child)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        _deny()


def _known_request(connection, request):
    """Only an atomic, definitely unstarted human action may survive restart."""
    from .agent_resource_store import encoded
    grants = list(connection.execute('SELECT * FROM agent_resource_grants WHERE request_id=?', (request['id'],)))
    actions = {r['action_id']: r['request_digest'] for r in connection.execute(
        'SELECT * FROM agent_resource_actions WHERE request_id=?', (request['id'],))}
    if request['status'] == 'pending':
        return (request['version'] == 1 and not request.get('approval') and not request.get('action_receipts')
                and not request.get('_intent') and not grants and not actions
                and request['continuation'] == {'state': 'waiting_approval', 'can_resume': False, 'blocker': None})
    intent, approval = request.get('_intent') or {}, request.get('approval') or {}
    rejected = request.get('kind') in {'local_asset_permission','creative_resources'} and request['status'] == 'rejected'
    rejection = request.get('rejection') or {}
    if rejected:
        if ((request.get('kind')=='local_asset_permission' and request.get('resources') != []) or approval or grants
                or set(rejection) != {'action_id','basis','rejected_at'} or not rejection['rejected_at']
                or not isinstance(rejection['basis'],str) or not rejection['basis'].strip()): return False
    if (not (request['status'] == 'approved' or rejected) or set(intent) != {'id', 'state'} or not intent['id']
            or intent['state'] != 'prepared'
            or request['continuation'] != {'state': 'ready', 'can_resume': True, 'blocker': None}
            or not rejected and (set(approval) != {'id', 'action_id', 'approved_at', 'approved', 'basis'}
                or not approval['id'] or not approval['approved_at'] or not approval['basis'].strip())): return False
    expected = [] if rejected else [{'resource': r['resource'], 'scope': r['scope'], 'delta': r['requested_delta']} for r in request['resources']]
    if sorted(approval.get('approved', []), key=encoded) != sorted(expected, key=encoded): return False
    expected_grants = sorted((r['resource'], r['scope'], request['run_id'] if r['scope'] == 'run' else request['task_id'], r['delta'])
                             for r in expected)
    if sorted((r['resource'], r['scope'], r['scope_id'], r['delta']) for r in grants) != expected_grants: return False
    # Accepted actions form the original 1 -> approved -> optional explicit
    # resume chain. A started transition adds a version without a human receipt.
    receipts = request['action_receipts']
    if not receipts or len(receipts) != len(actions): return False
    seen = set()
    for version, receipt in enumerate(receipts, 1):
        action = ('reject' if rejected else 'approve') if version == 1 else 'resume'
        if (set(receipt) != {'action_id', 'action', 'expected_version', 'resulting_version', 'status'}
                or receipt['action'] != action or receipt['status'] != 'accepted'
                or receipt['expected_version'] != version or receipt['resulting_version'] != version + 1
                or receipt['action_id'] in seen): return False
        seen.add(receipt['action_id'])
        body = {'request_id': request['id'], 'expected_version': version, 'action_id': receipt['action_id']}
        if action == 'approve':
            if approval['action_id'] != receipt['action_id']: return False
            body.update(approved=sorted(approval['approved'], key=encoded), basis=approval['basis'].strip())
        elif action == 'reject':
            if rejection['action_id'] != receipt['action_id']: return False
            body['basis'] = rejection['basis'].strip()
        if actions.get(receipt['action_id']) != digest({'action': action, 'body': body}): return False
    return request['version'] == len(receipts) + 1


def safe_waiting_parent(manager, parent):
    """Fail closed before startup persists any interruption classification."""
    try:
        if (parent.get('operation') != 'conversation' or parent.get('status') != 'waiting_child'
                or parent.get('stage') != 'director_waiting' or parent.get('error')
                or parent.get('cancel_requested') or not parent.get('agent_resource_base')): return False
        child = manager.runs.get(parent.get('active_child_id'))
        if (not child or child.get('project_id') != parent['project_id'] or child.get('status') != 'awaiting_resume'
                or child.get('stage') not in {'resource_wait', 'asset_generation', 'asset_generation_blocked'}
                or child.get('error') or child.get('cancel_requested')): return False
        if guard_parent(manager, child) is not parent: return False
        if child['stage'] in {'asset_generation','asset_generation_blocked'}:
            if _failed_asset_wait(manager, child): return True
        if child['stage'] == 'asset_generation':
            from .local_resource_warning import safe_waiting
            return safe_waiting(manager, child)
        if child['stage'] == 'asset_generation_blocked': return False
        return _resource_wait(manager,parent,child)
    except Exception:
        # This is only recognition of an exception to startup's existing unknown
        # path. No exception permits work, clears errors, or creates a receipt.
        return False


def _resource_wait(manager,parent,child):
    from .agent_resource_store import ResourceStore
    state = manager._projects().state_store(child['project_id'])
    store = ResourceStore(state)
    request = store.request(run_id=child['id'])
    if (not request or request['parent_run_id'] != parent['id']
            or request['task_id'] != parent['director_task_id'] or request['project_id'] != parent['project_id']
            or request['run_id'] != child['id']): return False
    with state._connection() as c:
        if not _known_request(c, request): return False
    cp = store.checkpoint(request['checkpoint']['id'])
    from .bootstrap_checkpoint import verify
    from .builder_continuation import load
    verify(manager, child, cp)
    load(manager, child, cp)
    project = manager._projects().get(parent['project_id'], verify_artifacts=False)
    if project['revision'] != cp['expected_project_revision']: return False
    if child.get('bootstrap') and project.get('current_scene') is not None:
        pending = cp.get('agent_state', {}).get('_pending_judge') or {}
        selected = cp.get('selected_revision')
        if pending.get('mode') != 'finalize' or cp.get('pending_revision') or not selected: return False
        operation = manager._scene_operation(child['id'], selected, 'initial', prepare=False)
        found = state.lookup_receipt(operation['operation_id'])
        receipt = found.get('receipt') or {}
        if (found['status'] != 'committed' or found.get('artifact_status') != 'available'
                or receipt.get('kind') != 'initial' or receipt.get('run_id') != child['id']
                or receipt.get('project_id') != child['project_id']
                or receipt.get('target_selection') != project['current_scene']
                or receipt.get('working_version') != project['workspace']['working_version']
                or receipt.get('project_revision') != cp['expected_project_revision']
                or project['current_scene'].get('run_id') != child['id']
                or project['current_scene'].get('revision_id') != selected): return False
    return True


def _failed_asset_wait(manager, child):
    """Read original failure, checkpoint and paired exchange; never launch work."""
    from .asset_run_store import AssetRunStore
    from .asset_run import AssetRunHost
    from .local_asset_failure import confirmed
    from .asset_builder import exchange
    state = manager._projects().state_store(child['project_id'])
    store = AssetRunStore(state); doc = store.request(child['id'])
    if (not doc or doc['phase'] not in {'generation_blocked','failure_ready'}
            or doc.get('driver_error') not in {None,'asset_generation_failed'}
            or not confirmed(state,doc)): return False
    continuation = store.continuation(doc['id'])
    if continuation and continuation['state'] != 'accepted': return False
    AssetRunHost(None,None,None,None,None,None).verify(manager,child['id'],doc)
    cp = doc['checkpoint']
    if exchange(cp['native_exchange']) != cp['native_exchange'] or digest(cp['native_exchange']) != cp['native_exchange_digest']:
        return False
    if doc.get('bootstrap'):
        from .agent_resource_store import ResourceStore
        from .builder_continuation import load
        saved = ResourceStore(state).checkpoint(cp['continuation_checkpoint'])
        if exchange(load(manager,child,saved)) != cp['native_exchange']: return False
    else:
        rd = state.local_round(child['id'],round_id=doc['round_id'])
        if rd['status'] != 'paused' or rd['checkpoint'].get('request_id') != doc['id']: return False
    return True


def reconcile_local_failure_parent(manager, parent, *, action_id=None, finishing_child=None):
    """Only a trusted recover action / terminal callback may correct this bug.

    All actual model attempts must have returned. This never resumes a child,
    clears a task pause, repairs a ledger, or rewrites the old interruption.
    """
    try:
        with manager.lock:
            error = parent.get('error') or {}
            if (not (action_id or finishing_child) or error.get('code') != 'server_restarted'
                    or error.get('outcome_unknown') is not True or parent.get('cancel_requested')
                    or parent.get('director_ledger_unknown')): return False
            interruptions = [r for r in parent.get('events',[]) if r.get('stage') == 'interrupted']
            if len(interruptions) != 1 or interruptions[0].get('message') != error.get('message'): return False
            host = manager._director_host(); store = host.store(parent['project_id'])
            children = [manager.runs.get(r['child_id']) for r in store.bindings(parent['id'])]
            candidates = [r for r in children if r and r.get('bootstrap')]
            if len(candidates) != 1: return False
            child = candidates[0]
            if (not child or not child.get('bootstrap') or child.get('cancel_requested')
                    or child.get('director_ledger_unknown') or (child.get('error') or {}).get('outcome_unknown')
                    or child.get('status') not in {'awaiting_resume','completed','failed'}): return False
            for run in (parent,*children):
                if not run: return False
                worker = manager.workers.get(run['id'])
                if worker and worker.is_alive():
                    import threading
                    if not (run is child and finishing_child == child['id'] and worker is threading.current_thread()): return False
            if _parent(manager,child,reconcile=True) is not parent: return False
            from .asset_run_store import AssetRunStore
            from .asset_job_store import AssetJobStore
            from .asset_local_authorization import confirmed_local_resource_failure
            from .bootstrap_asset import verify_checkpoint_files
            from .builder_continuation import load
            from .agent_resource_store import ResourceStore
            from .asset_builder import exchange
            state = manager._projects().state_store(child['project_id'])
            doc = AssetRunStore(state).request(child['id'])
            if (not doc or doc.get('import_intent') or doc.get('import_receipt')
                    or doc.get('builder_completion') == 'unknown' or doc.get('attempt_history')
                    or not confirmed_local_resource_failure(state,doc,AssetJobStore(state).read(doc['job_id']),
                                                           open_wait=True,require_exit=True)): return False
            # Verify original files/identity without requiring today's selection
            # to equal the old candidate: the same Builder may have progressed.
            cp = ResourceStore(state).checkpoint(doc['checkpoint']['continuation_checkpoint'])
            verify_checkpoint_files(manager,child,doc,cp)
            if exchange(load(manager,child,cp)) != doc['checkpoint']['native_exchange']: return False
            if (child.get('agent') or {}).get('uncertain_write'): return False
            if child['status'] == 'awaiting_resume':
                if child.get('stage') not in {'asset_generation','asset_generation_blocked'} or not _failed_asset_wait(manager,child): return False
            original = copy.deepcopy(error)
            restored = copy.deepcopy(parent)
            if restored['status'] != 'completed': restored.update(status='waiting_child',stage='director_waiting')
            restored['error'] = None
            manager._event(restored,restored['stage'],'已核实原本地生成失败及父子调用：恢复原等待；原中断记录保留。')
            restored['events'][-1]['reconciliation'] = {'kind':'local_asset_parent_startup',
                'source':'protected_human_recover' if action_id else 'confirmed_child_finished',
                'action_id':action_id,'child_run_id':child['id'],'request_id':doc['id'],'job_id':doc['job_id'],
                'interruption_seq':interruptions[0]['seq'],'original_error':original}
            manager._save(restored)
            parent.clear(); parent.update(restored)
            return True
    except Exception:
        return False
