"""Advisory headroom and one original local job's protected human consent."""
import copy
from dataclasses import dataclass
import json
import math
import uuid

from .asset_provider import AssetJobError, digest, identifier
from .project_state_store import _now

HUMAN = object()
RECOMMENDED = {'available_ram_mib': 6144, 'commit_headroom_mib': 8192, 'available_vram_mib': 6144}
ADVISORY_CODES = {None, 'local_resource_monitor_unavailable', 'local_system_memory_insufficient',
                  'local_gpu_memory_insufficient'}
WAITING_ERRORS = {None, 'asset_local_resource_wait', 'asset_local_resource_warning'}


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def report(raw):
    raw = raw if isinstance(raw, dict) else {}
    def field(section, name):
        return _number((raw.get(section) or {}).get(name)) if isinstance(raw.get(section), dict) else None
    ram = field('ram', 'available')
    total, used = field('commit', 'commit_limit'), field('commit', 'commit_total')
    commit = total - used if total is not None and used is not None and total >= used else None
    gpu_total = gpu_free = None
    try:
        values = raw['gpu']['memory_mib'].splitlines()[0].split(',')
        gpu_total, gpu_free = map(float, values[:2])
        if _number(gpu_total) is None or _number(gpu_free) is None or gpu_free > gpu_total:
            gpu_total = gpu_free = None
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        gpu_total = gpu_free = None
    values = {'available_ram_mib': ram / 2**20 if ram is not None else None,
              'commit_headroom_mib': commit / 2**20 if commit is not None else None,
              'available_vram_mib': gpu_free, 'total_vram_mib': gpu_total}
    if isinstance(raw.get('snapshot'), dict):
        values = {key: _number(raw['snapshot'].get(key)) for key in values}
        if (values['available_vram_mib'] is not None and values['total_vram_mib'] is not None
                and values['available_vram_mib'] > values['total_vram_mib']):
            values['available_vram_mib'] = values['total_vram_mib'] = None
    names = {'available_ram_mib': 'low_ram', 'commit_headroom_mib': 'low_commit_headroom',
             'available_vram_mib': 'low_vram'}
    reasons = [names[key] for key, threshold in RECOMMENDED.items()
               if values[key] is not None and values[key] < threshold]
    if any(values[key] is None for key in RECOMMENDED):
        reasons.append('monitor_unavailable')
    return {'snapshot': values, 'recommended': dict(RECOMMENDED), 'reasons': reasons}


def scope(doc, job):
    return {'project_id': doc['project_id'], 'run_id': doc['run_id'], 'request_id': doc['id'],
        'job_id': job['job_id'], 'semantic_digest': job['semantic_digest'], 'wire_digest': doc['wire_digest'],
        'profile_digest': doc['profile_digest'], 'profile_id': doc['request']['profile']['id'],
        'profile_revision': doc['request']['profile']['revision'], 'reference_sha256': doc['reference']['sha256'],
        'grant_id': doc['grant_id'], 'authorization': copy.deepcopy(doc['local_authorization']),
        'stage_permission_digest': digest(doc.get('stage_permission'))}


def unsent(doc, job):
    if (doc['closed'] or doc['phase'] != 'generation' or doc.get('driver_error') not in WAITING_ERRORS
            or job['dispatch'] != 'prepared' or job['creation_attempts'] or job['task_id']
            or job['local'] not in {'waiting', 'active'} or job.get('safe_error')):
        raise AssetJobError('asset_resource_admission_conflict')


def _job(connection, job_id):
    row = connection.execute('SELECT document FROM asset_generation_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        raise AssetJobError('asset_resource_admission_conflict')
    return json.loads(row[0])


def _permission(store, connection, doc, job):
    from .asset_job_store import AssetJobStore
    from .local_asset_permission import check
    store.check_checkpoint(connection, doc)
    AssetJobStore._check_local_grant(connection, job)
    cap = store.capability_in(connection, doc['run_id'])
    if cap and cap.get('permission_policy'):
        check(connection, store.project, doc['run_id'], doc.get('stage_permission') or {},
              cap, reference=doc['reference'])


def guard(manager, run_id):
    from .resource_parent_recovery import guard_parent
    run = manager.runs[run_id]
    if run.get('error') or run.get('cancel_requested') or run.get('status') in {
            'interrupted', 'failed', 'cancelled', 'completed'}:
        raise AssetJobError('asset_resource_admission_conflict')
    try:
        guard_parent(manager, run)
    except Exception as error:
        if getattr(error, 'code', None) in {'resource_parent_unknown', 'resource_parent_unverified'}:
            raise AssetJobError('asset_resource_parent_unverified') from None
        raise


def safe_waiting(manager, child):
    """Read-only startup proof for this new, definitely unstarted warning."""
    from .asset_run import AssetRunHost
    from .asset_job_store import AssetJobStore
    from .asset_jobs import controlled_path, file_facts, MAX_IMAGE_BYTES
    from .asset_builder import exchange
    from .asset_run_store import AssetRunStore
    state = manager._projects().state_store(child['project_id'])
    store = AssetRunStore(state)
    doc = store.request(child['id'])
    if (not doc or not doc.get('builder_mode') or not doc.get('resource_warning')
            or doc.get('import_intent') or doc.get('import_receipt') or store.continuation(doc['id'])
            or doc['request']['profile']['provider_id'] != 'hunyuan'):
        return False
    job = AssetJobStore(state).read(doc['job_id'])
    unsent(doc, job)
    warning = doc['resource_warning']
    if warning['binding'] != scope(doc, job):
        return False
    if warning['status'] == 'pending':
        if warning.get('approval') or warning.get('action_digest'):
            return False
    elif warning['status'] == 'approved':
        approval = warning.get('approval') or {}
        version = warning.get('approved_request_version')
        if (set(approval) != {'action_id', 'approved_at'} or not approval['approved_at']
                or type(version) is not int or version >= doc['version']
                or warning.get('action_digest') != digest({'request_id': doc['id'],
                    'expected_request_version': version, 'warning_id': warning['id'],
                    'action_id': approval['action_id'], 'acknowledged_risk': True})):
            return False
    else:
        return False
    with state._connection() as connection:
        _permission(store, connection, doc, job)
    # This verifier needs no provider/profile configuration and performs no IO
    # beyond original candidate and input evidence.
    AssetRunHost(None, None, None, None, None, None).verify(manager, child['id'], doc)
    cp = doc['checkpoint']
    if exchange(cp['native_exchange']) != cp['native_exchange'] or digest(cp['native_exchange']) != cp['native_exchange_digest']:
        return False
    if doc.get('bootstrap'):
        from .agent_resource_store import ResourceStore
        from .builder_continuation import load
        original = ResourceStore(state).checkpoint(cp['continuation_checkpoint'])
        if exchange(load(manager, child, original)) != cp['native_exchange']:
            return False
    else:
        rd = state.local_round(child['id'], round_id=doc['round_id'])
        if (rd['status'] != 'paused' or rd['version'] != cp['round_version']
                or rd['checkpoint'].get('request_id') != doc['id']):
            return False
    _, facts = file_facts(controlled_path(state.project_root, job['fixed_input']['path']), MAX_IMAGE_BYTES)
    return all(facts[key] == job['fixed_input'][key] for key in facts)


def preflight(host, manager, run_id, job, provider):
    """Persist a stable warning, or the evidence for this unsent job's admission."""
    guard(manager, run_id)
    store = host.store(manager, run_id)
    raw, blocked = provider.preflight()
    observed = report(raw)
    with store.project._write() as connection:
        doc = store.request(run_id, connection=connection)
        fresh = _job(connection, job['job_id'])
        unsent(doc, fresh)
        _permission(store, connection, doc, fresh)
        if manager.runs[run_id].get('cancel_requested'):
            raise AssetJobError('asset_resource_admission_conflict')
        binding = scope(doc, fresh)
        warning = doc.get('resource_warning')
        if warning and warning['binding'] != binding:
            raise AssetJobError('asset_resource_admission_conflict')
        changes = {}
        if blocked not in ADVISORY_CODES:
            stable = (report(doc['resource_snapshot']) if doc.get('driver_error') == 'asset_local_resource_wait'
                      and doc.get('resource_snapshot') is not None else observed)
            changes = {'driver_error': 'asset_local_resource_wait', 'resource_snapshot': stable}
            admitted = False
        elif observed['reasons'] and not (warning and warning['status'] == 'approved'):
            if not warning:
                warning = {'id': uuid.uuid4().hex, 'status': 'pending', **observed,
                           'approval': None, 'binding': binding}
            changes = {'resource_warning': warning, 'driver_error': 'asset_local_resource_warning',
                       'resource_snapshot': {key: warning[key] for key in ('snapshot', 'recommended', 'reasons')},
                       'resource_admission': None}
            admitted = False
        else:
            evidence = ({'source': 'protected_human', 'warning_id': warning['id'],
                         'action_id': warning['approval']['action_id'],
                         **{key: warning[key] for key in ('snapshot', 'recommended', 'reasons')}}
                        if warning and warning['status'] == 'approved'
                        else {'source': 'host_preflight', **observed})
            changes = {'resource_admission': {'binding': binding, **evidence},
                       'driver_error': None, 'resource_retry_after': None}
            admitted = True
        if any(doc.get(key) != value for key, value in changes.items()):
            doc.update(copy.deepcopy(changes))
            store._put(connection, doc, doc['version'])
    return admitted


def public(doc, job):
    warning = doc.get('resource_warning')
    if not warning:
        return None
    # A later healthy preflight may admit normally; retain the old warning in
    # SQL without displaying it as outstanding or inventing a human approval.
    if warning['status'] == 'pending' and (doc.get('resource_admission') or {}).get('source') == 'host_preflight':
        return None
    value = {key: copy.deepcopy(warning[key]) for key in ('id', 'status')}
    value.update(report(warning), approval=({key: warning['approval'][key] for key in
                 ('action_id', 'approved_at')} if warning.get('approval') else None))
    value['can_continue'] = False
    if doc['closed'] and (warning['status'] == 'pending' or doc.get('close_reason') in {'cancelled', 'user_end'}):
        value['status'] = 'cancelled'
    elif warning['status'] == 'pending':
        try:
            unsent(doc, job)
            value['can_continue'] = doc.get('driver_error') == 'asset_local_resource_warning'
        except AssetJobError:
            pass
    return value


def approve(manager, run_id, body, *, source=None):
    if source is not HUMAN:
        raise AssetJobError('asset_resource_human_required')
    fields = {'request_id', 'expected_request_version', 'warning_id', 'action_id', 'acknowledged_risk'}
    if (not isinstance(body, dict) or set(body) != fields or body['acknowledged_risk'] is not True
            or type(body['expected_request_version']) is not int):
        raise AssetJobError('invalid_asset_resource_confirmation')
    for key in ('request_id', 'warning_id', 'action_id'):
        identifier(body[key])
    host = manager._asset_host()
    store = host.store(manager, run_id)
    doc = store.request(run_id)
    warning = (doc or {}).get('resource_warning')
    if not doc or doc['id'] != body['request_id'] or not warning or warning['id'] != body['warning_id']:
        raise AssetJobError('asset_resource_confirmation_conflict')
    fingerprint = digest(body)
    if warning.get('approval'):
        if warning.get('action_digest') != fingerprint:
            raise AssetJobError('asset_resource_confirmation_conflict')
        return host.public(manager, run_id)  # Read an accepted ACK; never schedule here.
    guard(manager, run_id)
    host.verify(manager, run_id, doc)
    host._profile(manager, run_id)
    jobs = host.coordinator(manager, run_id)
    job = jobs.store.read(doc['job_id'])
    jobs._action_facts(job)
    with manager.lock, store.project._write() as connection:
        current = store.request(run_id, connection=connection)
        fresh = _job(connection, doc['job_id'])
        unsent(current, fresh)
        _permission(store, connection, current, fresh)
        warning = current.get('resource_warning')
        if (manager.runs[run_id].get('cancel_requested') or not warning
                or warning['id'] != body['warning_id'] or warning['binding'] != scope(current, fresh)):
            raise AssetJobError('asset_resource_confirmation_conflict')
        if warning.get('approval'):
            if warning.get('action_digest') != fingerprint:
                raise AssetJobError('asset_resource_confirmation_conflict')
        else:
            if current['version'] != body['expected_request_version'] or warning['status'] != 'pending':
                raise AssetJobError('asset_resource_confirmation_conflict')
            warning.update(status='approved', action_digest=fingerprint,
                           approved_request_version=current['version'],
                           approval={'action_id': body['action_id'], 'approved_at': _now()})
            current.update(resource_retry_after=None)
            store._put(connection, current, current['version'])
    host._after_step('resource_warning_approved', current)
    host.sync(manager, run_id)
    return host.public(manager, run_id)


@dataclass(frozen=True)
class Admission:
    """Internal host evidence, never accepted from model tool/wire fields."""
    document: dict


def for_job(coordinator, job):
    state = coordinator.store.project
    with state._connection() as connection:
        row = connection.execute('SELECT run_id FROM asset_run_requests WHERE job_id=?', (job['job_id'],)).fetchone()
        if row:
            from .asset_run_store import AssetRunStore
            store = AssetRunStore(state)
            doc = store.request(row[0], connection=connection)
            evidence = doc.get('resource_admission')
            _permission(store, connection, doc, job)
            if doc['closed'] or not evidence or evidence['binding'] != scope(doc, job):
                raise AssetJobError('asset_resource_admission_conflict')
            warning = doc.get('resource_warning')
            if evidence['source'] == 'protected_human' and (
                    not warning or warning['status'] != 'approved'
                    or evidence['action_id'] != warning['approval']['action_id']
                    or evidence['warning_id'] != warning['id']):
                raise AssetJobError('asset_resource_admission_conflict')
            return Admission(copy.deepcopy(evidence))
    return None  # Standalone coordinator has no human-warning override.


def normal(provider, identity, profile, wire):
    raw, blocked = provider.preflight()
    observed = report(raw)
    if blocked not in ADVISORY_CODES:
        raise AssetJobError(blocked)
    if observed['reasons']:
        raise AssetJobError('asset_local_resource_confirmation_required')
    return Admission({'source': 'host_preflight', **observed, 'binding': {
        'project_id': identity.project_id, 'job_id': identity.job_id,
        'semantic_digest': identity.semantic_digest, 'wire_digest': digest(wire),
        'profile_id': profile.get('id'), 'profile_revision': profile.get('revision'),
        'reference_sha256': wire['input_sha256']}})


def validate(evidence, identity, profile, wire):
    if not isinstance(evidence, Admission):
        raise AssetJobError('asset_resource_admission_conflict')
    value = evidence.document
    expected = {'project_id': identity.project_id, 'job_id': identity.job_id,
        'semantic_digest': identity.semantic_digest, 'wire_digest': identity.wire_digest,
        'profile_id': profile.get('id'), 'profile_revision': profile.get('revision'),
        'reference_sha256': wire['input_sha256']}
    if (digest(wire) != identity.wire_digest or any(value['binding'].get(k) != v for k, v in expected.items())
            or value['source'] not in {'host_preflight', 'protected_human'}
            or value['source'] == 'host_preflight' and value['reasons']
            or value['source'] == 'protected_human' and (not value.get('warning_id') or not value.get('action_id'))):
        raise AssetJobError('asset_resource_admission_conflict')
    return copy.deepcopy(value)
