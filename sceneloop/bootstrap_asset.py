"""Initial-candidate adaptation of the existing AssetRun ledger.

No synthetic local round and no Working publication: the regular bootstrap
self-check/publication path remains the only initial Working writer.
"""
import copy
import json
import uuid

from .asset_provider import AssetJobError, digest, canonical_json
from .project_state_store import _now


def check_checkpoint(c, doc):
    cp = doc['checkpoint']
    row = c.execute('SELECT project_id,manifest FROM project_state WHERE singleton=1').fetchone()
    project = json.loads(row['manifest']) if row else None
    goal = c.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()
    goal = json.loads(goal[0]) if goal else None
    if (not project or row['project_id'] != doc['project_id'] or doc['round_id'] != doc['run_id']
            or project['revision'] != cp['start']['project_revision']
            or project['workspace']['working_version'] != 0 or project['current_scene'] is not None
            or not goal or goal['id'] != cp['goal_id'] or goal['content_digest'] != cp['goal_digest']):
        raise AssetJobError('asset_checkpoint_drift')


def request_document(host, manager, run, body, agent, exchange, inspected, request, request_digest, pose, reference, native):
    from .agent_resource_store import ResourceStore
    from .bootstrap_checkpoint import verify as verify_candidate
    from .builder_continuation import load
    state = host.store(manager,run['id']).project
    cp = ResourceStore(state).checkpoint(body.get('continuation_checkpoint'))
    if not cp or cp['agent_state'] != agent or agent.get('pending_revision'):
        raise AssetJobError('asset_checkpoint_drift')
    verify_candidate(manager,run,cp); load(manager,run,cp)
    project = manager._projects().get(run['project_id'],verify_artifacts=False)
    goal = state.refinement_state(run['id'])['goal']
    identity = uuid.uuid5(uuid.UUID(run['project_id']),f"{run['id']}:asset-slot:1").hex
    artifacts = manager._fixed_scene_artifacts(run,agent['selected_revision']) if agent['selected_revision'] else []
    return {'schema_version':1,'bootstrap':True,'builder_mode':True,'project_id':run['project_id'],
        'run_id':run['id'],'round_id':run['id'],'slot':1,'id':identity,'version':1,'request_revision':1,
        'submission_key':uuid.uuid5(uuid.UUID(identity),'submission').hex,
        'request_digest':request_digest,'request':copy.deepcopy(request),**inspected,
        'wire_digest':digest(inspected['semantic']['wire_template']),'model':request['profile']['model'],
        'endpoint':request['profile']['base_url'],'pose':pose,'local_goal':body['reason'],'focus_items':[],
        'job_id':None,'grant_id':None,'price_approval':None,'price_check':None,'cost_policy':'actual_usage',
        'reference':reference,'local_authorization':host.store(manager,run['id']).capability(run['id'])['local_authorization'],
        'stage_permission':copy.deepcopy(body['stage_permission']), 'reason':body['reason'],
        'phase':'approval','closed':False,'import_intent':None,'import_receipt':None,
        'checkpoint':{'round_id':run['id'],'round_version':0,'continuation_checkpoint':cp['id'],
            'start':{'project_revision':project['revision'],'working_version':0,'selection':None},
            'selected_revision':agent['selected_revision'],'pending_revision':None,
            'goal_id':goal['id'],'goal_digest':goal['content_digest'],'criteria_digest':None,
            'artifacts':artifacts,'working_artifacts':[],'start_artifacts':[],
            'agent_state':copy.deepcopy(agent),'usage':copy.deepcopy(run['usage']),
            'limits':copy.deepcopy(run['resource_budget']),'exchange_digest':digest(exchange),
            'native_exchange':native,'native_exchange_digest':digest(native)}}


def verify(host, manager, run_id, doc):
    from .agent_resource_store import ResourceStore
    from .project_runtime import file_sha256, is_link
    store = host.store(manager,run_id)
    run = manager.get(run_id)
    with store.project._connection() as c: check_checkpoint(c,doc)
    if run.get('cancel_requested'): raise AssetJobError('asset_wait_ended')
    if any(run['usage'].get(k,0)<v for k,v in doc['checkpoint']['usage'].items() if type(v) in (int,float)):
        raise AssetJobError('asset_checkpoint_drift')
    cp = ResourceStore(store.project).checkpoint(doc['checkpoint']['continuation_checkpoint'])
    return verify_checkpoint_files(manager,run,doc,cp)


def verify_checkpoint_files(manager, run, doc, cp):
    """Historical file/native identity only; not admission to resume or publish."""
    from .project_runtime import file_sha256, is_link
    run_id=run['id']
    if (not cp or cp['run_id'] != run_id or cp['project_id'] != doc['project_id']
            or doc['run_id'] != run_id or run['project_id'] != doc['project_id']
            or cp['id'] != doc['checkpoint']['continuation_checkpoint']
            or cp['base_digest'] != run['agent_resource_base']['digest']
            or cp['agent_state'] != doc['checkpoint']['agent_state'] or cp['usage'] != doc['checkpoint']['usage']):
        raise AssetJobError('asset_checkpoint_drift')
    root = manager.run_directory(run_id)
    for item in [cp['manifest'], *cp['files']]:
        path = (root/item['path']).resolve()
        if not path.is_relative_to(root) or any(is_link(p) for p in [path,*path.parents] if p.is_relative_to(root)) or not path.is_file() or file_sha256(path)!=item['sha256']:
            raise AssetJobError('asset_checkpoint_artifact_drift')
    manifest = json.loads((root/cp['manifest']['path']).read_text(encoding='utf-8'))
    if manifest != {k:v for k,v in cp.items() if k not in {'manifest','native'}}:
        raise AssetJobError('asset_checkpoint_drift')
    return doc


def freeze_import(store, c, current, agent_state, pose):
    if (not isinstance(agent_state,dict) or not agent_state.get('selected_revision') or agent_state.get('pending_revision')
            or current['closed'] or current['phase'] != 'builder_continuing'):
        raise AssetJobError('asset_import_not_admissible')
    if current.get('import_agent_state') and (current['pose'] != pose or current['import_agent_state'] != agent_state):
        raise AssetJobError('asset_import_identity_conflict')
    current.update(pose=copy.deepcopy(pose),import_agent_state=copy.deepcopy(agent_state))
    return store._put(c,current,current['version'])


def admit_import(store, c, current, step, identity, limits):
    # All bootstrap attempts (regular edits, imports and transforms) share the
    # existing resource vector; a failed/unknown attempt is still an attempt.
    used = c.execute('SELECT COUNT(*) FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=?', (current['run_id'],)).fetchone()[0]
    if used >= limits['max_edit_attempts']: raise AssetJobError('round_resource_exhausted')
    if c.execute("SELECT 1 FROM operations o JOIN edit_attempts a ON o.operation_id=a.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')", (current['run_id'],)).fetchone():
        raise AssetJobError('asset_import_outcome_unknown')
    c.execute("INSERT INTO operations VALUES (?,?,?,?,?,'prepared',?,?,0)",
        (step['operation_id'],current['project_id'],current['run_id'],'asset_import',canonical_json(identity),_now(),_now()))
    c.execute("INSERT INTO edit_attempts(attempt_id,operation_id,retry_index,status,created_at) VALUES (?,?,0,'scheduled',?)",
        (step['attempt_id'],step['operation_id'],_now()))
    current.update(import_intent=identity,phase='importing')
    return store._put(c,current,current['version']),True


def finalize_import(store,c,current,receipt):
    identity = current['import_intent']
    row = c.execute('SELECT status FROM edit_attempts WHERE attempt_id=? AND operation_id=?',(identity['attempt_id'],identity['operation_id'])).fetchone()
    if not row or row[0] not in {'scheduled','unknown'}: raise AssetJobError('asset_import_receipt_conflict')
    c.execute("UPDATE edit_attempts SET status='succeeded',retryable=0,failure_category=NULL,detail=NULL WHERE attempt_id=?",(identity['attempt_id'],))
    c.execute("UPDATE operations SET state='prepared',updated_at=? WHERE operation_id=?",(_now(),identity['operation_id']))
    current.update(import_receipt=receipt,import_history=current['closed'])
    if not current['closed']: current['phase']='pending_review'
    return store._put(c,current,current['version'])


def fail_import(store,c,doc,confirmed_rejected):
    intent=doc['import_intent'];outcome='failed' if confirmed_rejected else 'unknown'
    c.execute("UPDATE edit_attempts SET status=?,retryable=0,failure_category=? WHERE attempt_id=?",
        (outcome,'confirmed_rejected' if confirmed_rejected else 'write_unknown',intent['attempt_id']))
    c.execute('UPDATE operations SET state=?,updated_at=? WHERE operation_id=?',
        ('prepared' if confirmed_rejected else 'unknown',_now(),intent['operation_id']))
    if not doc['closed']:doc['phase']='import_failed' if confirmed_rejected else 'import_unknown'
    return store._put(c,doc,doc['version'])


def finish_confirmed(manager, run):
    """Idempotent bookkeeping after a proven initial completion, including restart.

    Never resumes a model/write and never changes a failed or unknown Run.
    """
    if (not run.get('bootstrap') or run.get('status') != 'completed' or run.get('error')
            or run.get('cancel_requested') or run.get('director_ledger_unknown')): return False
    from .asset_run_store import AssetRunStore
    from .asset_job_store import AssetJobStore
    state=manager._projects().state_store(run['project_id']);store=AssetRunStore(state)
    doc=store.request(run['id'])
    if not doc or not doc.get('bootstrap') or doc.get('driver_error') or doc.get('builder_completion')=='unknown': return False
    if doc['closed']: return doc.get('close_reason')=='builder_end'
    selected=(run.get('agent') or {}).get('selected_revision')
    if not selected or run['agent'].get('pending_revision'): return False
    with state._connection() as c:
        receipts=state.receipts_for_run(run['id'],connection=c)
        if not any(r['kind']=='initial' and r.get('target_selection',{}).get('revision_id')==selected for r in receipts): return False
        if c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='unknown'",(run['id'],)).fetchone(): return False
        if c.execute("SELECT 1 FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')",(run['id'],)).fetchone(): return False
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_attempts'").fetchone():
            for row in c.execute('SELECT document FROM director_attempts'):
                attempt=json.loads(row[0])
                if attempt.get('run_id')==run['id'] and (attempt.get('state')!='settled' or attempt.get('cost_conflict')): return False
    job=AssetJobStore(state).read(doc['job_id']) if doc['job_id'] else None
    from .asset_local_authorization import confirmed_local_resource_job
    from .local_asset_failure import confirmed
    known_failure=bool(doc.get('failure_feedback') and confirmed(state,doc))
    if not known_failure and (not job or job['dispatch']!='accepted' or job['remote']!='succeeded' or job['preparation']!='ready'
            or job['safe_error'] is not None or not (job['cost'] in {'consumed','no_charge_confirmed','released'}
                or confirmed_local_resource_job(state,doc,job))): return False
    if doc.get('import_intent') and not doc.get('import_receipt'): return False
    store.finish_resume(doc['id'])
    manager._close_asset_wait(run['id'],reason='builder_end')
    return True
