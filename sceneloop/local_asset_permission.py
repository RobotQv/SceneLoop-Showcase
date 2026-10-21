"""Round-bound local permission in the existing resource request ledger.

Capability, task quota and permission are separate. Nothing in this module can
approve a model request or create a provider job.
"""
import copy
import json

from .asset_provider import AssetJobError, digest


def scope(run, state, *, connection=None):
    if run.get('cancel_requested') or run.get('status') in {'completed', 'failed', 'cancelled', 'interrupted'}:
        raise AssetJobError('asset_permission_scope_expired')
    if run.get('bootstrap'):
        return {'scope_kind': 'bootstrap', 'scope_id': run['id']}
    if connection is None:
        with state._connection() as c: return scope(run, state, connection=c)
    row = connection.execute('SELECT document FROM local_rounds WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run['id'],)).fetchone()
    rd = json.loads(row[0]) if row else None
    if not rd or rd['status'] not in {'active', 'paused'}:
        raise AssetJobError('asset_permission_round_required')
    return {'scope_kind': 'local_round', 'scope_id': rd['id']}


def details(manager, run, value):
    if (not isinstance(value, dict) or set(value) != {'reference_id', 'purpose', 'generation_count'}
            or value['generation_count'] != 1 or type(value['generation_count']) is not int
            or not isinstance(value['purpose'], str) or not 1 <= len(value['purpose'].strip()) <= 2000):
        raise AssetJobError('asset_permission_invalid')
    host = manager._asset_host()
    cap = host._profile(manager, run['id'])
    if cap['profile']['provider_id'] != 'hunyuan':
        raise AssetJobError('asset_permission_local_only')
    reference = host.reference_resolver(manager, run['id'], value['reference_id'])
    if reference['project_id'] != run['project_id'] or reference['purpose'] != 'asset':
        raise AssetJobError('asset_reference_identity_conflict')
    return {'provider_id': 'hunyuan', **scope(run, host.store(manager, run['id']).project),
        'generation_count': 1, 'purpose': value['purpose'].strip(),
        'profile': {k: cap['profile'][k] for k in ('id', 'revision', 'model')},
        'reference': {'artifact_id': reference['reference_id'], 'sha256': reference['sha256']}}


def _requests(connection, project, run_id, cap):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_resource_requests'").fetchone():
        return []
    result = []
    for row in connection.execute('SELECT id,document FROM agent_resource_requests WHERE run_id=? ORDER BY rowid DESC', (run_id,)):
        request = json.loads(row['document'])
        if request.get('kind') != 'local_asset_permission': continue
        if (request.get('id') != row['id'] or request.get('run_id') != run_id
                or request.get('project_id') != project.project_id
                or request.get('task_id') != cap['local_authorization']['task_id']):
            raise AssetJobError('asset_permission_identity_conflict')
        result.append(request)
    return result


def _summary(request):
    return {'permission_request_id':request['id'], 'status':request['status'],
            'permission':copy.deepcopy(request['permission'])}


def bind(manager, run, reference_id, reason, request_id=None):
    """Choose an immutable application; never reinterpret reason as its purpose."""
    expected = details(manager, run, {'reference_id':reference_id, 'purpose':reason, 'generation_count':1})
    host = manager._asset_host(); store = host.store(manager, run['id'])
    cap = host._profile(manager, run['id'])
    identity = {k:v for k,v in expected.items() if k != 'purpose'}
    with store.project._connection() as c:
        requests = _requests(c, store.project, run['id'], cap)
        candidates = [r for r in requests if {k:v for k,v in r['permission'].items() if k != 'purpose'} == identity]
        if request_id is not None:
            candidates = [r for r in candidates if r['id'] == request_id]
            if not candidates: raise AssetJobError('asset_permission_identity_conflict')
        elif len(candidates) > 1:
            raise AssetJobError('asset_permission_ambiguous', 'Choose one frozen permission_request_id; reason cannot select or change its purpose.',
                details={'candidates':[_summary(r) for r in candidates],
                         'guidance':'Use permission_request_id for the intended frozen purpose. A new intent requires an explicit request_resources application.'})
        if candidates:
            request = candidates[0]
            permission = {**copy.deepcopy(request['permission']), 'request_id':request['id']}
            reference = host.reference_resolver(manager, run['id'], reference_id)
            # An existing denial is a decision, not an invitation to auto-apply.
            check(c, store.project, run['id'], permission, cap, reference=reference)
            return permission
    return expected


def check(connection, project, run_id, permission, cap, *, reference, require=True):
    """Read authority under the same SQL lock as the eventual generation claim."""
    if (not isinstance(permission,dict) or not cap or cap.get('project_id') != project.project_id
            or cap.get('run_id') != run_id or not cap.get('local_authorization')
            or permission.get('provider_id') != 'hunyuan' or type(permission.get('generation_count')) is not int or permission.get('generation_count') != 1
            or permission.get('profile') != {k: cap['profile'][k] for k in ('id', 'revision', 'model')}
            or permission.get('reference') != {'artifact_id': reference['reference_id'], 'sha256': reference['sha256']}):
        raise AssetJobError('asset_permission_identity_conflict')
    if permission['scope_kind'] == 'bootstrap':
        if permission['scope_id'] != run_id or not cap.get('bootstrap'):
            raise AssetJobError('asset_permission_scope_expired')
        manifest = json.loads(connection.execute('SELECT manifest FROM project_state WHERE singleton=1').fetchone()[0])
        if manifest['current_scene'] is not None or manifest['workspace']['working_version'] != 0:
            raise AssetJobError('asset_permission_scope_expired')
    elif permission['scope_kind'] == 'local_round':
        row = connection.execute('SELECT status FROM local_rounds WHERE run_id=? AND round_id=?', (run_id, permission['scope_id'])).fetchone()
        if not row or row[0] not in {'active', 'paused'}: raise AssetJobError('asset_permission_scope_expired')
    else:
        raise AssetJobError('asset_permission_scope_expired')
    requests = _requests(connection, project, run_id, cap)
    request_id = permission.get('request_id')
    frozen = {k:v for k,v in permission.items() if k != 'request_id'}
    if 'request_id' in permission:
        bound = next((r for r in requests if r['id'] == request_id), None)
        if not bound or bound['permission'] != frozen:
            raise AssetJobError('asset_permission_identity_conflict')
        # Preserve the existing newest-decision rule for the exact frozen intent.
        # An older ID must not bypass a later refusal of that same application.
        for request in requests:
            if request['permission'] == frozen and request['status'] in {'rejected', 'cancelled', 'blocked'}:
                if require: raise AssetJobError('asset_stage_permission_required')
                return None
            if request['id'] == request_id: break
        if bound['status'] == 'approved' and bound.get('approval'):
            return {'source':'resource_request', 'request_id':request_id, 'approval_id':bound['approval']['id']}
        if require: raise AssetJobError('asset_stage_permission_required')
        return None
    for request in requests:
        if request['permission'] != permission: continue
        if request['status'] == 'approved' and request.get('approval'):
            return {'source': 'resource_request', 'request_id': request['id'], 'approval_id': request['approval']['id']}
        if request['status'] in {'rejected', 'cancelled', 'blocked'}:
            if require: raise AssetJobError('asset_stage_permission_required')
            return None
    initial = cap.get('initial_permission')
    if initial and initial['reference'] == permission['reference'] and initial['generation_count'] == 1:
        first = connection.execute('SELECT round_id FROM local_rounds WHERE run_id=? ORDER BY rowid LIMIT 1', (run_id,)).fetchone()
        if permission['scope_kind'] == 'bootstrap' or first and first[0] == permission['scope_id']:
            return {'source': 'protected_user_request', 'authorization_id': cap['local_authorization']['authorization_id']}
    if require: raise AssetJobError('asset_stage_permission_required')
    return None


def authorized(manager, run, permission, *, require=True):
    host = manager._asset_host(); store = host.store(manager, run['id'])
    cap = host._profile(manager, run['id'])
    actual = scope(run, store.project)
    if any(permission.get(k) != v for k, v in actual.items()): raise AssetJobError('asset_permission_scope_expired')
    reference = host.reference_resolver(manager, run['id'], permission['reference']['artifact_id'])
    with store.project._connection() as c:
        return check(c, store.project, run['id'], permission, cap, reference=reference, require=require)


def projection(manager, run):
    result = {'provider_id': 'hunyuan', 'supported_stages': ['bootstrap', 'local_round'],
        'capability_is_permission': False, 'requires': ['saved_local_profile', 'trusted_asset_reference', 'task_quota', 'current_stage_permission', 'runtime_resource_preflight'],
        'status': 'configuration_required', 'generation_limit_per_run': 1}
    if not run.get('asset_capability'): return result
    host = manager._asset_host(); cap = host.store(manager, run['id']).capability(run['id'])
    if cap['profile']['provider_id'] != 'hunyuan': return result
    result.update(status='available_requires_permission', profile={k: cap['profile'][k] for k in ('id', 'revision', 'model')})
    result['references'] = [{k:r[k] for k in ('artifact_id','sha256','purpose')} for r in run.get('reference_snapshots',[]) if r['purpose']=='asset']
    try: result['current_scope'] = scope(run,host.store(manager,run['id']).project)
    except AssetJobError as error: result['scope_status'] = error.code
    from .asset_local_authorization import LocalAssetAuthority
    auth = LocalAssetAuthority(host.store(manager, run['id']).project).read(**cap['local_authorization'], allow_stopped=True)
    result['task_quota'] = {k: auth[k] for k in ('effective_max_generations', 'used_generations', 'stopped')}
    result['initial_stage_permission'] = copy.deepcopy(cap.get('initial_permission'))
    store = host.store(manager, run['id'])
    with store.project._connection() as c:
        requests = _requests(c, store.project, run['id'], cap)
    result['permissions'] = [_summary(r) for r in requests]
    for item in result['permissions']:
        current = result.get('current_scope')
        item['applies_to_current_stage'] = bool(current and all(item['permission'].get(k) == v for k,v in current.items()))
    if requests: result['latest_permission'] = copy.deepcopy(result['permissions'][0])
    return result
