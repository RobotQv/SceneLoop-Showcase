"""Confirmed local failure feedback and scoped next-attempt authority.

The original Job and native tool exchange remain immutable. These are current
facts, not a fabricated tool reply or a refund of the failed generation.
"""
import copy
import json
import uuid

from .asset_provider import AssetJobError, digest


def confirmed(state, doc):
    from .asset_job_store import AssetJobStore
    from .asset_local_authorization import confirmed_local_resource_failure
    if (not doc or doc['closed'] or not doc.get('builder_mode') or not doc.get('job_id')
            or doc.get('import_intent') or doc.get('import_receipt')
            or doc.get('builder_completion')=='unknown'):
        return False
    with state._connection() as c:
        if (c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='unknown'",(doc['run_id'],)).fetchone()
                or c.execute("SELECT 1 FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')",(doc['run_id'],)).fetchone()):return False
        counter=c.execute('SELECT needs_reconciliation FROM run_counters WHERE run_id=?',(doc['run_id'],)).fetchone()
        if counter and counter[0]:return False
    job=AssetJobStore(state).read(doc['job_id'])
    return confirmed_local_resource_failure(state,doc,job,open_wait=True,require_exit=True)


def mark_ready(host, manager, run_id):
    store=host.store(manager,run_id);doc=store.request(run_id);run=manager.get(run_id)
    if not doc or doc.get('driver_error') not in {None,'asset_generation_failed'}:return False
    if (run.get('cancel_requested') or run.get('director_ledger_unknown')
            or (run.get('error') or {}).get('outcome_unknown') or (run.get('agent') or {}).get('uncertain_write')
            or run['status'] not in {'awaiting_resume','running'}):return False
    worker=manager.workers.get(run_id)
    if worker and worker.is_alive():return False
    continuation=store.continuation(doc['id']) if doc else None
    if continuation and continuation['state']!='accepted':return False
    if not confirmed(store.project,doc):return False
    host.verify(manager,run_id,doc)
    from .hunyuan_provider import HunyuanProvider
    job=host.local_coordinator(manager,run_id).store.read(doc['job_id'])
    evidence=HunyuanProvider(store.project.project_root).progress(job)
    feedback={'request_id':doc['id'],'job_id':doc['job_id'],'outcome':'confirmed_failed',
        'process_exited':True,'imported':False,'generation_consumed':1,'local_process':evidence,
        'next_actions':['continue_procedurally','request_resources_for_new_local_attempt'],
        'original_job_must_not_replay':True}
    if doc['phase']!='failure_ready':
        store.update(doc,phase='failure_ready',driver_error=None,failure_feedback=feedback)
        host.sync(manager,run_id)
    return True


def resume_guard(manager,store,doc):
    run=manager.get(doc['run_id'])
    if (run.get('cancel_requested') or run.get('director_ledger_unknown')
            or (run.get('error') or {}).get('outcome_unknown') or (run.get('agent') or {}).get('uncertain_write')
            or not confirmed(store.project,doc)):
        raise AssetJobError('asset_local_failure_unconfirmed')


def binding(doc):
    return {'project_id':doc['project_id'],'run_id':doc['run_id'],'round_id':doc['round_id'],
        'request_id':doc['id'],'job_id':doc['job_id'],'profile_digest':doc['profile_digest'],
        'reference':copy.deepcopy(doc['reference']),'stage_permission':copy.deepcopy(doc['stage_permission'])}


def approved_retry(state, doc, *, connection=None):
    if connection is None:
        with state._connection() as c:return approved_retry(state,doc,connection=c)
    for row in connection.execute('SELECT document FROM agent_resource_requests WHERE run_id=? ORDER BY rowid DESC',(doc['run_id'],)):
        request=json.loads(row[0])
        if request.get('_local_asset_retry')!=binding(doc):continue
        if request['status']!='approved' or not request.get('approval'):return None
        grant=connection.execute("SELECT delta FROM agent_resource_grants WHERE request_id=? AND resource='local_generations' AND scope='task' AND scope_id=?",(request['id'],request['task_id'])).fetchone()
        if grant and grant[0]==1:return request
        return None
    return None


def retry_identity(doc, approval):
    return uuid.uuid5(uuid.UUID(doc.get('intent_id',doc['id'])),'retry:'+approval['id']).hex


def verified_attempt_identity(state, doc, initial_id):
    """Read-only projection accepts only the original, approved retry chain."""
    from .asset_run_store import AssetRunStore
    try:
        history=doc.get('attempt_history') or []
        if not history:return doc['id']==initial_id and not doc.get('previous_attempt')
        if doc['request']['profile']['provider_id']!='hunyuan':return False
        store=AssetRunStore(state);previous=None;seen=set()
        for index,current in enumerate([r['document'] for r in history]+[doc]):
            if current['id'] in seen:return False
            seen.add(current['id'])
            if index<len(history) and (digest(current)!=history[index]['digest']
                    or store.request(request_id=current['id'])!=current):return False
            if index==0:
                if current['id']!=initial_id or current.get('previous_attempt'):return False
            else:
                approval=approved_retry(state,previous)
                if (not approval or current.get('retry_approval_id')!=approval['id']
                        or current['id']!=retry_identity(previous,approval)
                        or current.get('intent_id')!=initial_id or current.get('attempt_index')!=index+1
                        or current.get('previous_attempt',{}).get('id')!=previous['id']
                        or any(current.get(k)!=previous.get(k) for k in ('run_id','project_id','round_id',
                            'semantic_digest','reference','stage_permission','local_authorization'))):return False
            if current['submission_key']!=uuid.uuid5(uuid.UUID(current['id']),'submission').hex:return False
            previous=current
        return True
    except (KeyError,TypeError,ValueError,AssetJobError):
        return False


def next_document(state, old, doc):
    if not confirmed(state,old):raise AssetJobError('asset_local_failure_unconfirmed')
    approval=approved_retry(state,old)
    if not approval:raise AssetJobError('asset_local_retry_approval_required')
    if (doc['semantic_digest']!=old['semantic_digest'] or doc['reference']!=old['reference']
            or doc['stage_permission']!=old['stage_permission'] or doc['round_id']!=old['round_id']):
        raise AssetJobError('asset_retry_intent_conflict')
    doc.update(id=retry_identity(old,approval),intent_id=old.get('intent_id',old['id']),
        attempt_index=old.get('attempt_index',1)+1,retry_approval_id=approval['id'],
        previous_attempt={'id':old['id'],'digest':digest(old)})
    doc['submission_key']=uuid.uuid5(uuid.UUID(doc['id']),'submission').hex
    return doc
