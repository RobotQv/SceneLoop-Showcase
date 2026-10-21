"""SQL-bound unpublished construction lineage; never a second Working head."""
import copy
import json

from .director_contract import digest, reject


def deny(message, code='creative_continuation_blocked'):
    reject(message, code, 409)


def latest(store, *, exclude=None):
    # SQL insertion order is delegation order. Revision names are local to a Run.
    with store.state._connection() as c:
        rows=c.execute('SELECT document FROM director_delegations ORDER BY rowid DESC').fetchall()
    for row in rows:
        binding=json.loads(row[0])
        if binding['child_id'] != exclude:return binding
    return None


def audit(manager, run):
    """Verify every recorded batch is paired, without synthesizing private history."""
    from .builder_context import BusinessAudit
    from .project_runtime import is_link
    root=manager.run_directory(run['id']);path=root/'agent-trace.jsonl'
    if not path.is_file() or is_link(path) or (run.get('agent',{}).get('builder_audit') or {}).get('status')=='incomplete':
        deny('来源的工具审计不完整。')
    reader=BusinessAudit(root);pending={};seen=set();references=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        entry=json.loads(line);ref=entry.get('audit');record=reader.read(ref)
        if ref['sequence'] in seen or record['kind']!=entry['kind']:deny('来源审计身份不一致。')
        seen.add(ref['sequence']);kind=record['kind'];value=record['value']
        if kind=='assistant':
            if pending:deny('来源包含未完成工具批次。')
            calls=value.get('tool_calls') or []
            pending={call['id']:call['function']['name'] for call in calls}
            if len(pending)!=len(calls):deny('来源工具调用身份重复。')
        elif kind=='tool':
            if pending.pop(value['id'],None)!=value['name']:deny('来源工具配对不完整。')
        references.append(ref)
    if pending:deny('来源最后工具批次尚未完成。')
    memory=copy.deepcopy(run.get('agent',{}).get('builder_memory'))
    if memory and memory.get('version'):
        value=reader.read(memory['audit'])
        if value['kind']!='builder_memory' or value['value']!={k:v for k,v in memory.items() if k!='audit'}:
            deny('来源工作记忆与模型原记录不符。')
    return {'audit_digest':digest(references),'memory':memory}


def checked(runtime, parent, binding):
    """Known terminal failures may retain candidates; uncertain effects may not."""
    from .agent_resources import project as budget
    from .agent_resource_store import ResourceStore
    m=runtime.manager;store=runtime.store(parent['project_id']);task=store.task()
    if binding['task_id']!=task['task_id']:deny('候选不属于当前任务。')
    run=m.get(binding['child_id']);agent=run.get('agent') or {}
    pending_edit=run.get('operation')=='edit' and bool(agent.get('pending_revision'))
    if (run.get('project_id')!=parent['project_id'] or not (run.get('bootstrap') or pending_edit)
            or run.get('director_delegation',{}).get('delegation_id')!=binding['delegation_id']
            or run.get('director_ledger_unknown') or (run.get('error') or {}).get('outcome_unknown')
            or agent.get('uncertain_write') or (agent.get('pending_revision') and not pending_edit)):
        deny('最新施工存在未核实身份、调用或待定写入，不能退回旧候选。')
    revision=agent.get('pending_revision') if pending_edit else agent.get('selected_revision')
    if not revision:deny('最新委派没有可核验的选中候选，不能重建或退回旧版本。')
    mode='successor'
    ended_local_failure=False
    if run['status']=='interrupted' and run.get('stage')=='asset_user_end' and run.get('cancel_requested') and not run.get('error'):
        from .asset_run_store import AssetRunStore
        from .asset_job_store import AssetJobStore
        from .asset_local_authorization import confirmed_local_resource_failure
        stopped=AssetRunStore(store.state).request(run['id'])
        if (stopped and stopped['closed'] and stopped.get('close_reason')=='user_end'
                and stopped['phase']=='ended' and stopped.get('job_id')
                and not stopped.get('import_intent') and not stopped.get('driver_error')):
            ended_local_failure=confirmed_local_resource_failure(store.state,stopped,AssetJobStore(store.state).read(stopped['job_id']))
    if run['status'] not in {'completed','failed','cancelled'} and not ended_local_failure:
        from .resource_parent_recovery import safe_waiting_parent
        owner=m.runs.get(binding['parent_id'])
        if not owner or not safe_waiting_parent(m,owner):deny('原Run仍活跃或没有可安全恢复的原生检查点。')
        mode='same_run'
    elif binding['state']!='completed' or not binding.get('result'):
        deny('终态执行尚未交付确定的 SQL 委派结果。')
    worker=m.workers.get(run['id'])
    if worker and worker.is_alive():deny('来源施工仍在收尾。')
    state=store.state
    with state._connection() as c:
        if c.execute('SELECT 1 FROM reconciliation LIMIT 1').fetchone() or c.execute('SELECT 1 FROM run_counters WHERE needs_reconciliation!=0 LIMIT 1').fetchone():
            deny('项目正式保存状态待核对。')
        if (c.execute("SELECT 1 FROM operations WHERE state='unknown'").fetchone()
                or c.execute("SELECT 1 FROM edit_attempts WHERE status IN ('scheduled','unknown')").fetchone()):
            deny('项目存在未核实写入。')
        attempts=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_attempts WHERE task_id=?',(task['task_id'],))]
        if any(a['state']!='settled' or a.get('cost_conflict') or a.get('cost_anomaly') for a in attempts):
            deny('任务调用或费用尚未结算。')
        for role,key in (('builder','brain_calls'),('judge','judge_calls')):
            if len([a for a in attempts if a['run_id']==run['id'] and a['role']==role])!=run['usage'].get(key,0):
                deny('来源调用计数与 SQL 账本不一致。')
        for row in c.execute('SELECT document FROM director_decisions'):
            decision=json.loads(row[0])
            if decision['state'] in {'started','unknown'}:deny('任务存在未知 Director 决策。')
        for row in c.execute('SELECT document FROM asset_generation_jobs'):
            job=json.loads(row[0])
            cost_known=job['cost'] in {'consumed','released','no_charge_confirmed'}
            if not cost_known:
                from .asset_local_authorization import confirmed_local_resource_job, confirmed_local_resource_failure
                request=c.execute('SELECT document FROM asset_run_requests WHERE job_id=?',(job['job_id'],)).fetchone()
                if request:
                    doc=json.loads(request[0])
                    cost_known=confirmed_local_resource_job(state,doc,job) or confirmed_local_resource_failure(state,doc,job)
            if (job['dispatch'] in {'submitting','outcome_unknown'}
                    or job['remote'] not in {'succeeded','failed','banned','expired','cancelled'}
                    or not cost_known):
                deny('任务存在未结生成或费用。')
        if mode=='successor' and c.execute('SELECT 1 FROM asset_run_requests WHERE run_id=? AND closed=0',(run['id'],)).fetchone():
            deny('来源资产等待尚未结束。')
    version=m.scene_version(run['id'],revision,allow_active=mode=='same_run')
    files=[{k:copy.deepcopy(item[k]) for k in ('id','kind','path','sha256','revision_id')}
           for item in run['artifacts'] if item.get('revision_id')==revision and item['kind'] in {'blend','plan','inspection','render'}]
    # scene_version verifies every required source file; fixed-artifact validation
    # additionally checks identity of the selected main/auxiliary observation pair.
    fixed=m._fixed_scene_artifacts(run,revision)
    pending=None
    if pending_edit:
        from .refinement_round import criteria_snapshot
        document=state.local_round(run['id']);project=m.get_project(parent['project_id'])
        current=state.refinement_state()
        step=next((r for r in (document or {}).get('steps',[]) if r['revision_id']==revision),None)
        operation=state.operation_state(step['operation_id']) if step else None
        attempt=next((a for a in (operation or {}).get('attempts',[]) if a['attempt_id']==step['attempt_id']),None) if step else None
        if (run['status']!='failed' or not document or document['status']!='ended'
                or document['pending_revision']!=revision or not step or step['status']!='pending'
                or not operation or operation['state']!='prepared'
                or operation['identity'].get('run_id')!=run['id'] or operation['identity'].get('round_id')!=document['id']
                or not attempt or attempt['status']!='succeeded' or step['artifacts']!=fixed
                or document.get('receipt_operation_id') or state.receipts_for_run(run['id'])
                or document['start']['selection']!=project['current_scene']
                or document['start']['working_version']!=project['workspace']['working_version']
                or document['start']['project_revision']!=project['revision']
                or not current['goal'] or current['goal']['id']!=document['goal_id']
                or current['goal']['content_digest']!=document['goal_digest']
                or criteria_snapshot(current['plan'],document['item_ids'])!=document['criteria_items']):
            deny('待自检草稿缺少已终结局部轮、成功尝试或一致的Working/Goal证据。')
        pending={k:copy.deepcopy(document[k]) for k in ('id','version','goal_id','goal_digest','item_ids','criteria_items','start','local_goal','local_criteria')}
        pending['step']=copy.deepcopy(step)
        pending['operations']=[copy.deepcopy(op) for row in document['steps'] if row['status'] in {'selected','pending'} for op in row['operations']]
    evidence=audit(m,run)
    resources=budget(run,state);base=ResourceStore(state).base(run['id'])
    if not base or base.get('task_id')!=task['task_id']:deny('旧候选缺少冻结资源账本，不能补发默认额度。')
    return {'mode':mode,**({'kind':'pending_edit','pending_round':pending} if pending_edit else {}),'task_id':task['task_id'],'authorization_id':task['creative_authorization']['authorization_id'],
        'source':{'delegation_id':binding['delegation_id'],'parent_id':binding['parent_id'],'run_id':run['id'],
                  'scene_id':run['scene_id'],'revision_id':revision},
        'files':files,'fixed_artifacts':fixed,'usage':copy.deepcopy(run['usage']),
        'base_digest':run['agent_resource_base']['digest'],
        'resources':[copy.deepcopy(r) for r in resources['resources'] if r['scope']=='run'],
        'objects':copy.deepcopy(version['plan'].get('objects',[])),
        'assets':copy.deepcopy(version['plan'].get('assets',{})),**evidence}


def capture(runtime, parent):
    from .director_creation import authority
    m=runtime.manager;store=runtime.store(parent['project_id']);session=store.session(parent['id'])
    if 'creative_continuation' in session:return session['creative_continuation']
    project=m.get_project(parent['project_id'])
    if parent.get('request_protocol') not in {'natural_v1','natural_v2'} or parent.get('conversation_mode')=='read_only' or not authority(runtime,parent):return None
    binding=latest(store)
    if not binding:return None
    if project.get('current_scene'):
        source=m.get(binding['child_id'])
        if source.get('operation')!='edit' or source['status']!='failed' or not source.get('agent',{}).get('pending_revision'):return None
    from .runtime import AppError
    try:value=checked(runtime,parent,binding)
    except AppError as error:
        # Discussion can still report a blocked source. Execution never gets a
        # fallback candidate, repairable-parameter label, or fresh allowance.
        value={'mode':'blocked','task_id':store.task()['task_id'],
               'source':{'delegation_id':binding['delegation_id'],'run_id':binding['child_id']},
               'blocker':{'code':error.code,'outcome_unknown':bool(getattr(error,'outcome_unknown',False))}}
    value['baseline']={'selection':copy.deepcopy(project.get('current_scene')),'working_version':project['workspace']['working_version'],'project_revision':project['revision']}
    value['accepted_request_digest']=digest({'parent_id':parent['id'],'request_fingerprint':session['run']['request_fingerprint']})
    value['identity']=digest(value)
    session['creative_continuation']=value;store.save_session(session)
    return value


def current(runtime, parent):
    return (runtime.store(parent['project_id']).session(parent['id']) or {}).get('creative_continuation')


def verify(runtime, parent, frozen, *, child_id=None):
    if frozen['mode']=='blocked':deny('本请求绑定的最新来源不可安全继续；原记录保留，不能退回旧版本或重建。')
    project=runtime.manager.get_project(parent['project_id'])
    if (project.get('current_scene')!=frozen['baseline']['selection'] or project['workspace']['working_version']!=frozen['baseline']['working_version']
            or project['revision']!=parent['expected_project_revision']):deny('续做期间 Working/项目基线已变化。','creative_continuation_stale')
    binding=latest(runtime.store(parent['project_id']),exclude=child_id)
    if not binding or binding['delegation_id']!=frozen['source']['delegation_id']:deny('最新委派已改变；本请求不能覆盖。','creative_continuation_stale')
    value=checked(runtime,parent,binding)
    for key in value:
        if value[key]!=frozen[key]:deny('来源候选、调用或剩余额度已改变。','creative_continuation_stale')
    return value


def public(value):
    if value is None:return None
    if value['mode']=='blocked':return copy.deepcopy(value)
    return {k:copy.deepcopy(value[k]) for k in ('identity','mode','kind','pending_round','task_id','source','baseline','objects','assets','resources','usage','memory') if k in value}


def recover_scope_pause(runtime, parent, scope, *, prove_only=False):
    """A new accepted edit can replace only a proved parameter-only dead end."""
    store=runtime.store(parent['project_id']);task=store.task();pause=task.get('pause')
    from .director_request_scope import managed
    permitted=scope['intent']=='edit' or scope['intent']=='continuous' and (managed(runtime,parent) or prove_only)
    if (not pause or pause['reason']!='repeated_tool_error_no_progress' or pause.get('scope')!='autonomous'
            or not permitted or not scope.get('creative_continuation')
            or set(pause.get('evidence',{}).get('error_codes',[]))!={'director_scope_invalid'}):return
    origins=[]
    for session in store.sessions():
        rid=session['parent_id'];old=runtime.manager.runs.get(rid,{})
        if rid==parent['id'] or session.get('request_scope') or session.get('error_repeats',0)<2:continue
        if (old.get('status')!='awaiting_resume' or old.get('stage')!='director_paused'
                or old.get('error') or old.get('cancel_requested') or old.get('director_ledger_unknown')):continue
        if store.bindings(rid):continue
        with store.state._connection() as c:
            decisions=[json.loads(row[0]) for row in c.execute('SELECT document FROM director_decisions')
                       if json.loads(row[0])['parent_id']==rid]
        if len(decisions)<3:continue
        valid=True
        for decision in decisions:
            batch=decision.get('batch') or {};calls=decision.get('assistant',{}).get('tool_calls') or []
            outcomes=batch.get('outcomes') or []
            if (decision['state']!='applied' or not calls or len(outcomes)!=len(calls)
                    or batch.get('calls')!=[{'id':q['id'],'digest':digest(q)} for q in calls]):valid=False;break
            for call,outcome in zip(calls,outcomes):
                reply=json.loads(outcome.get('tool',{}).get('content','{}'))
                if (call['function']['name']!='director_request_scope' or set(outcome)!={'tool'}
                        or reply.get('ok') is not False or reply.get('error',{}).get('code')!='director_scope_invalid'):
                    valid=False;break
        if not valid:continue
        # This decrypts/validates the original latest complete native exchange.
        runtime._native_exchange(old,session)
        origins.append((rid,session,decisions))
    if len(origins)!=1:return
    rid,session,decisions=origins[0]
    verify(runtime,parent,scope['creative_continuation'])
    receipt={'source_pause':copy.deepcopy(pause),'source_parent_id':rid,'parent_id':parent['id'],
        'request_digest':scope['request_digest'],'scope_id':scope['id'],
        'continuation_identity':scope['creative_continuation']['identity'],
        'decision_ids':[d['decision_id'] for d in decisions],'private_ref':session['private_ref'],
        'execution':'confirmed_scope_only_no_effects'}
    if prove_only:return receipt
    from .director_store import encoded
    with store.state._write() as c:
        current=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        if current.get('pause')!=pause:deny('暂停身份已变化。')
        current.setdefault('creative_scope_recoveries',[]).append(receipt)
        current['pause']=None;current['revision']+=1
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(current),))


def copy_plan(lineage):
    return {'reason':'Continue verified pending draft; Builder review is still required.',
        'operations':[{'op':'copy_candidate','source':copy.deepcopy(lineage['source']),
                       'continuation_identity':lineage['identity']}]}
