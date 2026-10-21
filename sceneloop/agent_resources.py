"""Frozen base + SQL grants - cumulative actual usage, shared by all callers."""
import copy
import json
import uuid

from .agent_resource_store import ResourceStore
from .director_contract import digest, reject

SCHEMA_VERSION = 1
POLICY_ID = 'agent-resources-v1'
BOOTSTRAP = {'builder_calls': 25, 'draft_attempts': 6, 'tool_calls': 34,
             'observations': 18, 'judge_calls': 6}
FIELDS = {'builder_calls': 'max_brain_calls', 'draft_attempts': 'max_edit_attempts',
          'tool_calls': 'max_tool_calls', 'observations': 'max_observations',
          'judge_calls': 'max_judge_calls', 'saved_rounds': 'saved_round_limit'}
SCOPES = {key: {'run'} for key in BOOTSTRAP} | {'saved_rounds': {'run', 'task'}, 'local_generations': {'task'}}
HUMAN = object()  # Internal capability passed only by the protected HTTP handler.


def task_id(run):
    return run.get('director_delegation', {}).get('task_id') or run.get('director_task_id') or run['id']


def execution_source(runtime, parent, *, exclude=None):
    """The last proven terminal child supplies actual residual credits."""
    from .director_request_scope import managed
    if not managed(runtime,parent):return None
    store=runtime.store(parent['project_id'])
    with store.state._connection() as c:
        bindings=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_delegations WHERE parent_id=? ORDER BY rowid',(parent['id'],))]
    bindings=[r for r in bindings if r['child_id']!=exclude]
    if not bindings:return None
    record=bindings[-1];run=runtime.manager._get(record['child_id']);result=record.get('result')
    from .builder_terminal import blocker
    blocked=blocker(runtime,record)
    if blocked:
        reject('前序施工需核对：'+blocked['scope']+'/'+blocked['reason']+
            (' ('+str(blocked['claim_id'])+')' if blocked['claim_id'] else ''),'resource_source_unavailable',409)
    budget=project(run,store.state)
    if budget is None:reject('前序施工缺少冻结额度，不能补发默认值。','resource_source_unavailable',409)
    rows=[r for r in budget['resources'] if r['scope']=='run']
    if any(type(r['remaining']) is not int or r['remaining']<0 for r in rows):
        reject('前序施工剩余额度待核对。','resource_source_unavailable',409)
    base=ResourceStore(store.state).base(run['id'])
    grants=sum(r['delta'] for r in ResourceStore(store.state).grants('',record['task_id']) if r['scope']=='task' and r['resource']=='saved_rounds')
    if type(base.get('task_save_grants')) is not int or grants<base['task_save_grants']:
        reject('前序执行缺少可继承的task grant依据。','resource_source_unavailable',409)
    from .builder_terminal import excluded_assets
    source={'parent_id':parent['id'],'task_id':record['task_id'],'delegation_id':record['delegation_id'],
        'run_id':run['id'],'base_digest':run['agent_resource_base']['digest'],
        'result_digest':result['digest'],'resources':copy.deepcopy(rows),'usage':copy.deepcopy(run['usage']),
        'new_task_save_grants':grants-base['task_save_grants']}
    excluded=excluded_assets(runtime,record)
    if excluded:source['excluded_asset_jobs']=excluded
    return source


def freeze(run, state):
    if not run.get('project_id'): return
    if run.get('operation') == 'conversation':
        limits = {}  # Director calls are statistics, never a fixed call cap.
    elif not run.get('bootstrap'):
        from .refinement_round import enabled
        if not enabled(run) or run.get('local_round_limits', {}).get('schema_version') != 2: return
        from .refinement_budget import resolve_snapshot
        old = resolve_snapshot(run)
        limits = {name: old[field] for name, field in FIELDS.items()}
    else:
        limits = copy.deepcopy(BOOTSTRAP)
    lineage=run.get('director_delegation',{}).get('brief',{}).get('creative_continuation')
    if lineage:
        if (not run.get('bootstrap') and lineage.get('kind')!='pending_edit') or lineage['mode']!='successor' or lineage['task_id']!=task_id(run):
            reject('后继资源来源身份无效。','resource_base_conflict',409)
        limits={r['resource']:r['remaining'] for r in lineage['resources']}
        if set(limits)!=(set(BOOTSTRAP)|({'saved_rounds'} if lineage.get('kind')=='pending_edit' else set())) or any(type(v) is not int or v<0 for v in limits.values()):
            reject('原候选剩余额度不可确认，不能使用默认预算。','resource_base_conflict',409)
    execution=run.get('director_delegation',{}).get('brief',{}).get('execution_resources')
    if execution:
        if (execution['task_id']!=task_id(run) or execution['parent_id']!=run['director_delegation']['parent_id']):
            reject('同请求后继额度身份不一致。','resource_base_conflict',409)
        residual={r['resource']:r['remaining'] for r in execution['resources']}
        if 'saved_rounds' in residual:residual['saved_rounds']+=execution['new_task_save_grants']
        if not set(BOOTSTRAP)<=set(residual) or any(type(v) is not int or v<0 for v in residual.values()):
            reject('同请求剩余额度缺失。','resource_base_conflict',409)
        limits={key:min(value,residual[key]) if key in residual else value for key,value in limits.items()}
    base = {'schema_version': SCHEMA_VERSION, 'policy_id': POLICY_ID, 'run_id': run['id'],
            'project_id': run['project_id'], 'task_id': task_id(run),
            'parent_run_id': run.get('director_delegation', {}).get('parent_id') or run['id'],
            'request_fingerprint': run['request_fingerprint'], 'bootstrap': bool(run.get('bootstrap')),
            'limits': limits, 'completion_reserve': {'builder_calls': 2, 'tool_calls': 3, 'judge_calls': 1},
            'initial_publications': 1 if run.get('bootstrap') else 0}
    base['task_save_grants']=sum(r['delta'] for r in ResourceStore(state).grants('',task_id(run))
        if r['scope']=='task' and r['resource']=='saved_rounds')
    if lineage:
        base['continuation']={k:copy.deepcopy(lineage[k]) for k in ('identity','source','base_digest','resources','usage')}
    if execution:base['execution_source']=copy.deepcopy(execution)
    ResourceStore(state).freeze(run['id'], base)
    run['agent_resource_base'] = {'schema_version': SCHEMA_VERSION, 'digest': digest(base)}


def overlay(limits, budget):
    result = copy.deepcopy(limits)
    for row in (budget or {}).get('resources', []):
        if row['scope'] == 'run' and row['resource'] in FIELDS:
            result[FIELDS[row['resource']]] = row['effective_limit']
    return result


def round_allowance(run, state, *, connection=None):
    if connection is None:
        with state._connection() as c: return round_allowance(run, state, connection=c)
    # The projection needs identity/status, not the entire historical agent_state.
    current = connection.execute('SELECT round_id,status FROM local_rounds WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run['id'],)).fetchone()
    if not current or current['status'] not in {'active', 'paused'}: return None
    rows = connection.execute("SELECT g.delta,r.document FROM agent_resource_grants g JOIN agent_resource_requests r ON r.id=g.request_id WHERE g.scope='run' AND g.scope_id=? AND g.resource='draft_attempts'", (run['id'],))
    extra = sum(row['delta'] for row in rows if json.loads(row['document']).get('_round_id') == current['round_id'])
    return {'round_id': current['round_id'], 'extra_attempts': extra}


def task_limit(state, task, resource, base, *, connection=None):
    if base is None: return None
    store = ResourceStore(state)
    if not store.exists(): return base
    return base + sum(g['delta'] for g in store.grants('', task, connection=connection)
                      if g['scope'] == 'task' and g['resource'] == resource)


def request_save_boundary(connection, run_id):
    """Exact SQL delegation/scope bound; grants never alter the request's stop."""
    from .director_request_scope import managed_authorization
    from .refinement_round import RoundError
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_delegations'").fetchone():return None
    row=connection.execute('SELECT document FROM director_delegations WHERE child_id=?',(run_id,)).fetchone()
    if not row:return None
    binding=json.loads(row[0])
    if binding.get('brief',{}).get('execution_policy_version')!=2:return None
    session_row=connection.execute('SELECT document FROM director_sessions WHERE parent_id=?',(binding['parent_id'],)).fetchone()
    parent=connection.execute('SELECT task_id FROM director_parents WHERE parent_id=?',(binding['parent_id'],)).fetchone()
    session=json.loads(session_row[0]) if session_row else {};scope=session.get('request_scope')
    if (not parent or parent[0]!=binding['task_id'] or binding['child_id']!=run_id
        or not managed_authorization(session.get('run',{}),scope,session=session,connection=connection)):
        raise RoundError('原请求的SQL施工范围身份不可确认。','round_request_scope_invalid',409)
    stop=scope.get('stop_conditions')
    if not stop:return None
    counters=connection.execute('SELECT r.saved_edit_rounds,r.needs_reconciliation FROM run_counters r JOIN director_delegations d ON d.child_id=r.run_id JOIN director_parents p ON p.parent_id=d.parent_id WHERE p.task_id=?',(binding['task_id'],)).fetchall()
    if type(stop.get('total_saved_limit')) is not int or any(r[0] is None or r[1] for r in counters):
        raise RoundError('原请求保存停止条件或累计计数待核实。','round_request_scope_invalid',409)
    used=sum(r[0] for r in counters)
    return {'limit':stop['total_saved_limit'],'used':used,'remaining':max(0,stop['total_saved_limit']-used)}


def check_save_request(connection, run_id, resources):
    boundary=request_save_boundary(connection,run_id)
    if not boundary:return
    for row in resources:
        if row['resource']=='saved_rounds' and (boundary['remaining']==0 or row['scope']=='run'
            and row['proposed_limit']-row['used']>boundary['remaining']):
            from .visual_agent import ToolInputError
            raise ToolInputError('保存追加不能扩大原请求的停止条件；按现有余量完成并报告结果。',
                code='request_save_stop_limit',details={'total_saved_limit':boundary['limit'],
                    'used_saved_rounds':boundary['used'],'remaining_saved_rounds':boundary['remaining']})


def enforce_save(connection, run_id):
    """SQL transaction gate, independent of Run JSON/model projections."""
    import json
    from .refinement_round import RoundError
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_resource_bases'").fetchone(): return
    row = connection.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (run_id,)).fetchone()
    if not row: return  # Historical Runs keep their original contract.
    base = json.loads(row[0])
    if 'saved_rounds' not in base['limits']: return
    boundary=request_save_boundary(connection,run_id)
    if boundary and boundary['remaining']==0:
        raise RoundError('已达到原请求的保存停止条件；资源追加不能扩大该边界。','round_request_save_limit',409)
    counter = connection.execute('SELECT saved_edit_rounds,needs_reconciliation FROM run_counters WHERE run_id=?', (run_id,)).fetchone()
    used = counter[0] if counter else 0
    delta = connection.execute("SELECT COALESCE(SUM(delta),0) FROM agent_resource_grants WHERE scope='run' AND scope_id=? AND resource='saved_rounds'", (run_id,)).fetchone()[0]
    if used is None or counter and counter[1] or used >= min(12, base['limits']['saved_rounds'] + delta):
        raise RoundError('Run正式保存额度不足或计数待核实。', 'round_resource_exhausted', 409)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_tasks'").fetchone():
        row = connection.execute('SELECT document FROM director_tasks WHERE task_id=?', (base['task_id'],)).fetchone()
        if row:
            task = json.loads(row[0])
            limits = [n for n in (task['policy'].get('total_saved_limit'), (task.get('creative_authorization') or {}).get('approved', {}).get('total_saved_limit')) if type(n) is int]
            if limits:
                counters = connection.execute('SELECT r.saved_edit_rounds,r.needs_reconciliation FROM run_counters r JOIN director_delegations d ON d.child_id=r.run_id JOIN director_parents p ON p.parent_id=d.parent_id WHERE p.task_id=?', (base['task_id'],)).fetchall()
                delta = connection.execute("SELECT COALESCE(SUM(delta),0) FROM agent_resource_grants WHERE scope='task' AND scope_id=? AND resource='saved_rounds'", (base['task_id'],)).fetchone()[0]
                if any(r[0] is None or r[1] for r in counters) or sum(r[0] for r in counters) >= min(limits) + delta:
                    raise RoundError('本task原人类总保存额度不足；Run追加不能替代task批准。', 'round_task_save_limit', 409)


def attempt_limits(connection, operation, round_id, previous):
    """Resolve actual attempt limits inside the scheduling transaction."""
    import json
    from .refinement_round import RoundError
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_resource_bases'").fetchone(): return previous
    row = connection.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (operation['run_id'],)).fetchone()
    if not row: return previous
    base = json.loads(row[0])
    if 'draft_attempts' not in base['limits']: return previous
    grants = [(dict(g), json.loads(g['document'])) for g in connection.execute(
        "SELECT g.*,r.document FROM agent_resource_grants g JOIN agent_resource_requests r ON r.id=g.request_id WHERE g.scope='run' AND g.scope_id=? AND g.resource='draft_attempts'", (operation['run_id'],))]
    total = base['limits']['draft_attempts'] + sum(g['delta'] for g, _ in grants)
    if base['bootstrap']:
        used = connection.execute('SELECT COUNT(*) FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=?', (operation['run_id'],)).fetchone()[0]
        if used >= total: raise RoundError('初建草稿尝试已耗尽，需明确追加。', 'round_resource_exhausted', 409)
        return previous
    if round_id is None: return previous
    per_round = base['limits']['draft_attempts'] // max(1,base['limits']['saved_rounds'])
    per_round += sum(g['delta'] for g, request in grants if request.get('_round_id') == round_id)
    return {**(previous or {}), 'max_edit_attempts': total, 'max_edit_attempts_per_round': per_round}


def _task_dimensions(state, task, *, connection=None):
    if connection is None:
        with state._connection() as c: return _task_dimensions(state, task, connection=c)
    result = []
    c = connection
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'director_tasks' in tables:
        row = c.execute('SELECT document FROM director_tasks WHERE task_id=?', (task,)).fetchone()
        if row:
            doc = json.loads(row[0])
            creative = doc.get('creative_authorization', {}).get('approved', {})
            candidates = [v for v in (doc.get('policy', {}).get('total_saved_limit'), creative.get('total_saved_limit')) if type(v) is int]
            if candidates:
                counters = c.execute('SELECT r.saved_edit_rounds,r.needs_reconciliation FROM run_counters r JOIN director_delegations d ON d.child_id=r.run_id JOIN director_parents p ON p.parent_id=d.parent_id WHERE p.task_id=?', (task,)).fetchall()
                used = None if any(r[1] or r[0] is None for r in counters) else sum(r[0] for r in counters)
                result.append(('saved_rounds', min(candidates), used))
    if 'asset_local_authorizations' in tables:
        row = c.execute('SELECT document FROM asset_local_authorizations WHERE task_id=? ORDER BY rowid LIMIT 1', (task,)).fetchone()
        if row:
            auth = json.loads(row[0])
            used = c.execute('SELECT COUNT(*) FROM asset_local_grants g JOIN asset_local_authorizations a ON a.id=g.authorization_id WHERE a.task_id=?', (task,)).fetchone()[0]
            result.append(('local_generations', auth['max_generations'], used))
    return result


def project(run, state, *, agent_state=None, connection=None):
    if not run.get('agent_resource_base'): return None
    if connection is None:
        with state._connection() as c: return project(run, state, agent_state=agent_state, connection=c)
    store = ResourceStore(state)
    row = connection.execute('SELECT document FROM agent_resource_bases WHERE run_id=?', (run['id'],)).fetchone()
    base = json.loads(row[0]) if row else None
    if (not base or digest(base) != run['agent_resource_base'].get('digest')
            or base['project_id'] != run['project_id'] or base['task_id'] != task_id(run)
            or base['request_fingerprint'] != run['request_fingerprint']):
        reject('冻结资源基础与Run身份不一致。', 'resource_base_conflict', 409)
    grants = store.grants(run['id'], base['task_id'], connection=connection)
    used = (agent_state or run.get('agent') or {}).get('used', {})
    usage = run['usage']
    attempt_scope = '' if base['bootstrap'] else " AND o.kind IN ('draft_edit','asset_import','asset_transform')"
    attempts = connection.execute('SELECT COUNT(*) FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=?' + attempt_scope, (run['id'],)).fetchone()[0]
    counter = connection.execute('SELECT saved_edit_rounds FROM run_counters WHERE run_id=?', (run['id'],)).fetchone()
    counts = {'builder_calls': usage.get('brain_calls', 0), 'judge_calls': usage.get('judge_calls', 0),
              'tool_calls': used.get('tool_calls', 0), 'observations': used.get('observations', 0),
              'draft_attempts': attempts, 'saved_rounds': counter[0] if counter else 0}
    rows = [(key, 'run', cap, counts[key]) for key, cap in base['limits'].items()]
    rows += [(key, 'task', cap, count) for key, cap, count in _task_dimensions(state, base['task_id'], connection=connection)]
    resources = []
    for key, scope, cap, count in rows:
        effective = cap + sum(g['delta'] for g in grants if g['resource'] == key and g['scope'] == scope)
        if key == 'saved_rounds' and scope == 'run': effective = min(12, effective)
        resources.append({'resource': key, 'scope': scope, 'used': count, 'base_limit': cap,
                          'effective_limit': effective, 'remaining': None if count is None else max(0, effective - count)})
    return {'schema_version': SCHEMA_VERSION, 'policy_id': POLICY_ID,
            'resources': resources, 'grants_revision': len(grants)}


def hydrate(run, state, *, connection=None):
    """One fresh SQL connection per projection; no cross-request grant cache."""
    if connection is None:
        with state._connection() as c: return hydrate(run, state, connection=c)
    run['resource_budget'] = project(run, state, connection=connection)
    row = connection.execute('SELECT document FROM agent_resource_requests WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run['id'],)).fetchone()
    run['resource_request'] = ResourceStore.public(json.loads(row[0]) if row else None)
    from .refinement_round import enabled
    if enabled(run): run['resource_round_allowance'] = round_allowance(run, state, connection=connection)


def request_details(budget, resources):
    if not isinstance(resources, list) or not 1 <= len(resources) <= 8:
        reject('资源申请明细需要1–8项。', 'resource_request_invalid')
    rows = {(r['resource'], r['scope']): r for r in budget['resources']}
    seen = set()
    result = []
    for item in resources:
        if (not isinstance(item, dict) or set(item) != {'resource', 'scope', 'delta'}
                or not isinstance(item['resource'], str) or not isinstance(item['scope'], str)
                or item['scope'] not in SCOPES.get(item['resource'], set())
                or type(item['delta']) is not int or not 1 <= item['delta'] <= 10000):
            reject('资源名称、scope或正整数delta无效。', 'resource_request_invalid')
        key = item['resource'], item['scope']
        if key in seen or key not in rows: reject('资源重复或不属于当前授权。', 'resource_request_scope', 409)
        seen.add(key)
        row = rows[key]
        proposed = row['effective_limit'] + item['delta']
        if key == ('saved_rounds', 'run') and proposed > 12:
            reject('单Run正式保存不得超过12。', 'resource_saved_round_hard_limit', 409)
        result.append({k: copy.deepcopy(row[k]) for k in ('resource', 'scope', 'used', 'base_limit', 'effective_limit')} |
                      {'requested_delta': item['delta'], 'proposed_limit': proposed})
    return result


def _function(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


RESOURCE_ITEM = {'type': 'object', 'properties': {'resource': {'type': 'string', 'enum': list(SCOPES)},
    'scope': {'type': 'string', 'enum': ['run', 'task']}, 'delta': {'type': 'integer', 'minimum': 1}},
    'required': ['resource', 'scope', 'delta'], 'additionalProperties': False}
TOOLS = [_function('checkpoint_candidate', 'Freeze the current candidate and continuation without publishing Working or ending. The rest of this tool batch will not execute.',
    {'title': {'type': 'string'}}, ['title']),
    _function('request_resources', 'Request human approval for explicit additional execution resources. Never grants permission. Pauses this same Run after pairing all remaining tools as not executed.',
    {'reason': {'type': 'string'}, 'next_step': {'type': 'string'},
     'resources': {'type': 'array', 'items': RESOURCE_ITEM, 'minItems': 0, 'maxItems': 8},
     'local_asset_permission': {'type':'object','properties':{'reference_id':{'type':'string'},'purpose':{'type':'string'},
         'generation_count':{'type':'integer','enum':[1]}},'required':['reference_id','purpose','generation_count'],'additionalProperties':False}}, ['reason', 'next_step', 'resources'])]

INSTRUCTIONS = '''Use the independent resource_budget vector, not iteration.max_rounds, for bootstrap calls and draft edits.
checkpoint_candidate preserves selected AND unreviewed pending state without accepting, publishing or ending.
Only review_initial explicitly self-checks the selected initial candidate. finish then uses the original initial publication and independent Judge.
request_resources requests the stated positive deltas from a human and pauses this same Run; it cannot approve or change cost policy.
For local Hunyuan, request_resources may use resources=[] plus local_asset_permission {reference_id,purpose,generation_count:1}. This grants only the current bootstrap/local round, not task quota. A rejection returns to this same Builder: choose a procedural alternative or explain the limitation. Task local_generations quota is a separate positive-delta request.
Reserve the last calls/tools for self-check, finish, or requesting resources. Approved credits never reset usage, tool IDs, correction limits or unknown outcomes.'''


class ResourceRuntime:
    def __init__(self, manager):
        self.manager = manager

    def store(self, run):
        return ResourceStore(self.manager._projects().state_store(run['project_id']))

    def target(self, identifier):
        m = self.manager
        run = m._get(identifier)
        if run.get('operation') != 'conversation': return run
        store = m._director_host().store(run['project_id'])
        # Only the current trusted child of this exact parent/task is visible.
        own = self.store(run).request(run_id=identifier)
        active = next((b for b in store.bindings(identifier) if b['child_id'] == run.get('active_child_id')), None)
        if active:
            child = m.runs.get(active['child_id'])
            if child and child['project_id'] == run['project_id'] and active['task_id'] == run.get('director_task_id'):
                request = self.store(child).request(run_id=child['id'])
                if request and (request['continuation']['state'] not in {'completed', 'cancelled'}
                                or own and own['continuation']['state'] in {'completed','cancelled'}): return m._get(child['id'])
        if own and own['continuation']['state'] not in {'completed','cancelled'}: return run
        session=store.session(identifier) or {}
        scope=session.get('request_scope') or {}
        continuation=scope.get('creative_continuation')
        from .director_request_scope import managed
        if continuation and continuation['mode']=='same_run' and (scope.get('intent')=='edit' or managed(m._director_host(),run) and scope.get('intent')=='continuous'):
            child=m.runs.get(continuation['source']['run_id'])
            if child:
                binding=store.child(child['id'])
                if (binding and binding['delegation_id']==continuation['source']['delegation_id']
                        and binding['task_id']==run['director_task_id'] and child['project_id']==run['project_id']):
                    return child
        for binding in reversed(store.bindings(identifier)):
            child = m.runs.get(binding['child_id'])
            if child and binding['task_id'] == run.get('director_task_id') and child['project_id'] == run['project_id']:
                if self.store(child).request(run_id=child['id']): return m._get(child['id'])
        return run

    def view(self, identifier, *, receipt=None):
        run = self.manager._get(receipt['run_id']) if receipt else self.target(identifier)
        doc=receipt or self.store(run).request(run_id=run['id'])
        value={'resource_request':self.store(run).public(doc),'resource_budget':copy.deepcopy(run.get('resource_budget'))}
        if doc and identifier not in {doc['parent_run_id'],doc['run_id']}:
            session=self.manager._director_host().store(run['project_id']).session(identifier) or {}
            scope=session.get('request_scope') or {};continuation=scope.get('creative_continuation') or {}
            source=continuation.get('source') or {}
            if (scope.get('intent')=='edit' and continuation.get('mode')=='same_run'
                    and source.get('run_id')==doc['run_id'] and source.get('parent_id')==doc['parent_run_id']):
                value['continuation_alias']={'request_parent_run_id':identifier,'source_parent_run_id':doc['parent_run_id'],
                    'source_run_id':doc['run_id'],'project_id':doc['project_id'],'task_id':doc['task_id'],
                    'request_id':doc['id'],'continuation_id':continuation['identity']}
        return value

    def director_request(self, runtime, parent, body):
        if runtime.is_readonly(parent) or not parent.get('agent_resource_base'):
            reject('只读或旧请求不能申请执行资源。', 'resource_request_scope', 409)
        from .creative_continuation import current
        continuation=current(runtime,parent)
        if continuation and continuation['mode']=='same_run':
            reject('原Run已有安全等待申请；请保留其申请ID，由人类批准/拒绝后按原native续做。','resource_request_pending',409)
        if (not isinstance(body, dict) or set(body) != {'reason', 'next_step', 'resources'}
                or any(not isinstance(body[k], str) or not 1 <= len(body[k].strip()) <= 2000 for k in ('reason', 'next_step'))):
            reject('资源申请字段无效。', 'resource_request_invalid')
        if not isinstance(body['resources'], list) or any(not isinstance(r, dict) or r.get('scope') != 'task' for r in body['resources']):
            reject('Director此入口仅申请本task资源；当前child由其自身绑定申请。', 'resource_request_scope', 409)
        current = runtime.store(parent['project_id']).task()
        from .director_request_scope import enabled,pause_applies
        if (pause_applies(runtime,parent) if enabled(runtime,parent) else bool(current.get('pause'))):
            reject('原安全或费用暂停不能用追加额度清除。', 'resource_continuation_blocked', 409)
        resources = request_details(self.manager._get(parent['id'])['resource_budget'], body['resources'])
        return {'reason': body['reason'].strip(), 'next_step': body['next_step'].strip(), 'resources': resources}

    def seal_director(self, runtime, parent, exchange, details):
        from types import SimpleNamespace
        from .creative_continuation import current
        from .bootstrap_checkpoint import guard
        from .builder_continuation import seal
        from .project_state_store import _write_json
        from .project_runtime import file_sha256
        m = self.manager
        guard(m, parent)
        project_doc = m.get_project(parent['project_id'])
        cp = {'id': uuid.uuid4().hex, 'project_id': parent['project_id'], 'run_id': parent['id'], 'role': 'director',
            'revision_id': (project_doc.get('current_scene') or {}).get('revision_id'), 'verified': True,
            'scene_identity': {'selection': project_doc.get('current_scene'), 'project_revision': project_doc['revision']},
            'base_digest': parent['agent_resource_base']['digest'], 'usage': copy.deepcopy(parent['usage']),
            'correction': copy.deepcopy(parent.get('builder_tool_correction')),
            'expected_project_revision': parent['expected_project_revision'], 'files': []}
        path = m.run_directory(parent['id']) / 'candidate-checkpoints' / cp['id'] / 'manifest.json'
        from .project_runtime import is_link
        if any(is_link(p) for p in path.parents if p.is_relative_to(m.run_directory(parent['id']))):
            reject('检查点目录不能重定向。', 'resource_checkpoint_changed', 409)
        path.parent.mkdir(parents=True, exist_ok=False)
        _write_json(path, cp)
        cp['manifest'] = {'path': path.relative_to(m.run_directory(parent['id'])).as_posix(), 'sha256': file_sha256(path)}
        cp['native'] = seal(SimpleNamespace(manager=m, run=parent, identifier=parent['id'], history=exchange, correction=None), cp)
        store = self.store(parent)
        store.save_checkpoint(cp)
        previous = store.request(run_id=parent['id'])
        if previous and previous['continuation']['state'] == 'running': store.transition(previous['id'], 'completed', expected_intent='started')
        from .director_request_scope import managed
        store.create_request({'id': uuid.uuid4().hex, 'version': 1, 'project_id': parent['project_id'],
            'task_id': task_id(parent), 'parent_run_id': parent['id'], 'run_id': parent['id'], 'requester_role': 'director',
            'status': 'pending', **details,
            **({'kind':'creative_resources'} if current(runtime,parent) or managed(runtime,parent) else {}),
            'checkpoint': {'id': cp['id'], 'revision_id': cp['revision_id'], 'verified': True},
            'continuation': {'state': 'waiting_approval', 'can_resume': False, 'blocker': None}, 'approval': None, 'action_receipts': []})
        m._update(parent['id'], 'resource_wait', '本task资源申请与完整原生配对已保存；等待人类批准。', status='awaiting_resume')

    def checkpoint_tool(self, agent, arguments):
        agent._args(arguments, ['title'])
        title = agent._text(arguments['title'], 200)
        if not (agent.state['selected_revision'] or agent.state['pending_revision']):
            from .visual_agent import ToolInputError
            raise ToolInputError('尚无候选；先create_scene。')
        agent.resource_boundary = {'title': title, 'request': None}
        return {'checkpoint_pending': True, 'working_unchanged': True, 'ends_run': False}

    def request_tool(self, agent, arguments):
        agent._args(arguments, ['reason', 'next_step', 'resources'] + (['local_asset_permission'] if isinstance(arguments,dict) and 'local_asset_permission' in arguments else []))
        reason = agent._text(arguments['reason'])
        next_step = agent._text(arguments['next_step'])
        from .visual_agent import ToolInputError
        if 'local_asset_permission' in arguments:
            if arguments['resources'] != []:
                raise ToolInputError('本地轮次许可与任务资源追加分别申请；许可resources必须为空。')
            from .local_asset_permission import details, authorized
            permission = details(self.manager, self.manager.get(agent.identifier), arguments['local_asset_permission'])
            authority = authorized(self.manager, self.manager.get(agent.identifier), permission, require=False)
            if authority:
                return {'approved': True, 'permission': permission, 'same_run': agent.identifier, 'task_quota_unchanged': True,
                        **({'permission_request_id':authority['request_id']} if authority.get('request_id') else {})}
            identity = uuid.uuid4().hex
            agent.resource_boundary = {'title': reason[:200], 'request': {'id':identity, 'kind': 'local_asset_permission',
                'reason': reason, 'next_step': next_step, 'resources': [], 'permission': permission}}
            return {'requested': True, 'approved': False, 'permission_request_id':identity,
                    'permission': permission, 'same_run': agent.identifier}
        try:
            state=self.manager._projects().state_store(agent.run['project_id'])
            budget = project(self.manager.get(agent.identifier), state, agent_state=agent.state)
            resources = request_details(budget, arguments['resources'])
            with state._connection() as connection:check_save_request(connection,agent.identifier,resources)
        except Exception as error:
            if getattr(error, 'code', '').startswith('resource_request') or getattr(error, 'code', '') == 'resource_saved_round_hard_limit':
                raise ToolInputError(str(error)) from None
            raise
        agent.resource_boundary = {'title': reason[:200], 'request': {'reason': reason, 'next_step': next_step, 'resources': resources}}
        if any(r['resource']=='local_generations' for r in resources) and agent.run.get('asset_capability'):
            from .local_asset_failure import confirmed, binding
            doc=self.manager._asset_host().store(self.manager,agent.identifier).request(agent.identifier)
            if doc and doc.get('failure_feedback'):
                if not confirmed(state,doc):raise ToolInputError('原本地生成结果尚未确认，不能申请重放。')
                if any(r['requested_delta']!=1 for r in resources if r['resource']=='local_generations'):
                    raise ToolInputError('当前失败资产的新尝试仅申请一次；原失败不退款。')
                agent.resource_boundary['request']['_local_asset_retry']=binding(doc)
                # Reuse the existing approve/reject feedback continuation even
                # for older bootstrap delegations without policy_version=2.
                agent.resource_boundary['request']['kind']='creative_resources'
        return {'requested': True, 'approved': False, 'same_run': agent.identifier}

    def seal_boundary(self, agent):
        from . import bootstrap_checkpoint, builder_continuation
        boundary = agent.resource_boundary
        cp = bootstrap_checkpoint.capture(agent, boundary['title'], allow_empty=bool(boundary['request']))
        cp['native'] = builder_continuation.seal(agent, cp)
        store = self.store(agent.run)
        store.save_checkpoint(cp)
        agent.state['candidate_checkpoint'] = {'id': cp['id'], 'revision_id': cp['revision_id'], 'verified': True}
        if boundary['request']:
            details = boundary['request']
            run = self.manager.get(agent.identifier)
            previous = store.request(run_id=run['id'])
            if previous and previous['continuation']['state'] == 'running':
                store.transition(previous['id'], 'completed', expected_intent='started')
            doc = {'id': uuid.uuid4().hex, 'version': 1, 'project_id': run['project_id'],
                'task_id': task_id(run), 'parent_run_id': run.get('director_delegation', {}).get('parent_id') or run['id'],
                'run_id': run['id'], 'requester_role': 'builder', 'status': 'pending', **details,
                'checkpoint': {'id': cp['id'], 'revision_id': cp['revision_id'], 'verified': True},
                'continuation': {'state': 'waiting_approval', 'can_resume': False, 'blocker': None},
                'approval': None, 'action_receipts': []}
            brief=run.get('director_delegation',{}).get('brief',{})
            if brief.get('creative_continuation') or brief.get('execution_policy_version')==2:
                doc.setdefault('kind','creative_resources')
            if agent.local_round_mode:
                current = self.manager._projects().state_store(run['project_id']).local_round(run['id'])
                doc['_round_id'] = current['id'] if current and current['status'] == 'active' else None
            store.create_request(doc)
            message = ('编辑尚未初始化；待初始化状态已保存，等待人类批准资源后在原Run创建基线并观察。'
                       if agent.state.get('_pending_initialization') else '候选及原生配对已保存；等待人类批准资源，同Run续做。')
            agent._event('resource_wait', message, status='awaiting_resume')
            return True
        agent._event('candidate_checkpoint', '候选检查点已固定，Working未改变；继续当前Run。')
        agent.resource_boundary = None
        return False

    def exhausted(self, agent, resource):
        """No extra model call on exhaustion; preserve the last complete batch."""
        budget = project(self.manager.get(agent.identifier), self.manager._projects().state_store(agent.run['project_id']), agent_state=agent.state)
        rows = [{'resource': resource, 'scope': 'run', 'delta': 4 if resource == 'builder_calls' else 2}]
        agent.resource_boundary = {'title': '已耗尽执行资源', 'request': {
            'reason': '原冻结资源已耗尽，未额外调用模型。', 'next_step': '在当前候选上继续并完成自检/正式发布。',
            'resources': request_details(budget, rows)}}
        if agent.state.get('_pending_initialization'):
            agent.resource_boundary['request'].update(reason='原冻结余量不足以初始化本次编辑；尚未创建候选或调用Builder。',
                next_step='在原Run复制当前Working为独立基线、完成双视角观察后进入Builder。')
        return self.seal_boundary(agent)

    def action(self, identifier, action, body, *, source):
        if source is not HUMAN: reject('资源追加只接受受保护的人类入口。', 'resource_human_required', 403)
        m = self.manager
        with m.lock:
            run = self.target(identifier)
            store = self.store(run)
            doc = store.request(identity=body.get('request_id')) if isinstance(body, dict) and isinstance(body.get('request_id'), str) else None
            if doc and doc['run_id']!=run['id'] and doc['parent_run_id']==identifier:
                parent=m._get(identifier)
                if parent.get('operation')=='conversation' and doc['project_id']==parent['project_id'] and doc['task_id']==task_id(parent):
                    binding=m._director_host().store(parent['project_id']).child(doc['run_id'])
                    if doc['run_id']==identifier or binding and binding['parent_id']==identifier and binding['task_id']==doc['task_id']:
                        run=m._get(doc['run_id'])
            if not doc or doc['run_id'] != run['id'] or doc['task_id'] != task_id(run) or doc['project_id'] != run['project_id']:
                reject('申请不属于当前Run/task。', 'resource_request_scope', 409)
            # Validate shape, action binding, state and CAS BEFORE inspecting a
            # potentially old checkpoint. Invalid/obsolete clicks are read-only;
            # they must not turn a completed continuation back into blocked.
            _, fresh = store.action(doc['id'], action, body, validate_only=True)
            local_permission = doc.get('kind') == 'local_asset_permission'
            rejection_feedback = local_permission or doc.get('kind')=='creative_resources'
            if fresh and (action in {'approve', 'resume'} or action == 'reject' and rejection_feedback):
                try:
                    from .bootstrap_checkpoint import verify
                    from .builder_continuation import load
                    cp = store.checkpoint(doc['checkpoint']['id'])
                    verify(m, run, cp)
                    load(m, run, cp)
                    if doc.get('_local_asset_retry'):
                        from .local_asset_failure import confirmed, binding
                        current=m._asset_host().store(m,run['id']).request(run['id'])
                        if not confirmed(store.project,current) or binding(current)!=doc['_local_asset_retry']:
                            reject('原本地失败或轮次身份已变化，不能批准新尝试。','asset_retry_intent_conflict',409)
                    if local_permission:
                        from .local_asset_permission import details
                        permission = doc['permission']
                        actual = details(m, run, {'reference_id': permission['reference']['artifact_id'],
                            'purpose': permission['purpose'], 'generation_count': permission['generation_count']})
                        if actual != permission: reject('当前施工轮次或资产资源已变化。', 'asset_permission_scope_expired', 409)
                except Exception as error:
                    store.transition(doc['id'], 'blocked', blocker=getattr(error, 'code', 'resource_evidence_unavailable'))
                    raise
            doc, accepted = store.action(doc['id'], action, body)
            if accepted and action == 'reject' and not rejection_feedback:
                run['cancel_requested'] = True
                if run.get('director_delegation'):
                    m._director_host().store(run['project_id']).pause('resource_request_rejected', {'run_id': run['id'], 'request_id': doc['id']})
                m._cancelled(run['id'])
                worker = m.workers.get(run['id'])
                if not worker or not worker.is_alive(): m._worker_finished(run['id'])
            if accepted and (action in {'approve', 'resume'} or action == 'reject' and rejection_feedback):
                self.schedule(run, doc)
            if not accepted and doc['continuation']['state'] == 'ready':
                # Same action replay may finish a definitely unstarted intent,
                # but can neither create a second intent nor replay started work.
                self.schedule(run, doc)
            return self.view(identifier,receipt=store.request(identity=doc['id']))

    def schedule(self, run, doc):
        m = self.manager
        worker = m.workers.get(run['id'])
        if worker and worker.is_alive(): return
        if m._scene_workers_active(exclude=run['id']): return
        if doc.get('_intent', {}).get('state') != 'prepared': return
        m._start(run, self.execute)

    def execute(self, identifier):
        m = self.manager
        run = m._get(identifier)
        store = self.store(run)
        doc = store.request(run_id=identifier)
        try:
            from .bootstrap_checkpoint import verify
            cp = store.checkpoint(doc['checkpoint']['id'])
            verify(m, run, cp)
            from .builder_continuation import load
            load(m, run, cp)
            with m.lock:
                if not store.transition(doc['id'], 'running', expected_intent='prepared'): return
                run.update(status='running', stage='resource_continuing')
                if doc.get('kind')=='creative_resources':
                    run['creative_resource_feedback']={k:copy.deepcopy(doc[k]) for k in ('id','status','resources','approval','rejection') if k in doc}
                m._save(run)
            if run.get('operation') == 'conversation':
                m._director_host().execute(identifier)
            else:
                from .visual_agent import VisualAgent
                remaining=next((r['remaining'] for r in m.get(identifier)['resource_budget']['resources'] if r['resource']=='builder_calls' and r['scope']=='run'),None)
                if doc.get('kind')=='creative_resources' and doc['status']=='rejected' and remaining==0:
                    stopped=copy.deepcopy(cp['agent_state']);stopped['stop_reason']='capability_limit'
                    m._update(identifier,'resource_rejected','人类拒绝追加且 Builder 原余量为0；保留候选并交回 Director，未额外调用或发布。',
                              status='completed',agent=stopped,quality_status='needs_user_review')
                else:VisualAgent(m, identifier, resource_checkpoint=cp).execute()
            status = m.get(identifier)['status']
            if status == 'completed' and run.get('bootstrap'):
                from .bootstrap_asset import finish_confirmed
                finish_confirmed(m, run)
            asset_wait = (m.get(identifier).get('asset_request') or {}).get('phase') in {'approval','generation','ready'}
            if status in {'completed', 'failed', 'interrupted', 'cancelled'} or status == 'awaiting_resume' and asset_wait or run.get('operation') == 'conversation' and status in {'waiting_child', 'queued', 'awaiting_resume'}:
                store.transition(doc['id'], 'completed', expected_intent='started')
        except Exception as error:
            store.transition(doc['id'], 'blocked', blocker=getattr(error, 'code', 'resource_continuation_failed'))
            m._fail(identifier, error)

    def recover(self, run):
        if not run.get('agent_resource_base'): return
        store = self.store(run)
        doc = store.request(run_id=run['id'])
        if run.get('bootstrap') and run.get('status') == 'completed':
            from .bootstrap_asset import finish_confirmed
            if finish_confirmed(self.manager, run):
                if doc and doc.get('_intent',{}).get('state') == 'started':
                    store.transition(doc['id'],'completed',expected_intent='started')
                return
        if doc and doc.get('_intent', {}).get('state') == 'started':
            store.transition(doc['id'], 'blocked', blocker='resource_started_outcome_unknown')

    def cancel(self, run):
        if not run.get('agent_resource_base'): return
        store = self.store(run)
        doc = store.request(run_id=run['id'])
        if doc and doc['continuation']['state'] in {'waiting_approval', 'ready', 'running'}:
            store.transition(doc['id'], 'cancelled')
