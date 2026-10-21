"""Original Director response, ordered calls and durable once-only receipts."""
import copy
import json
from .director_contract import digest,reject
from . import director_reply_correction as correction

def observe(runtime,parent,event,call,body):
    from .observation_service import ObservationCache
    from .observation_contract import normalize_view_spec
    if not isinstance(body,dict) or set(body)!={'view'}:reject('观察只接受受控view。')
    try:view=normalize_view_spec(body['view'])
    except ValueError as error:reject(str(error),'observation_invalid')
    m=runtime.manager;project=m.get_project(parent['project_id'])
    if not project.get('current_scene'):reject('缺少Working观察来源。','working_scene_missing',409)
    if project['revision']!=parent['expected_project_revision']:reject('观察项目基线已变化。','director_stale',409)
    version=m.scene_version(project['current_scene']['run_id'],project['current_scene']['revision_id'])
    cache=ObservationCache(m,parent['project_id'],m.blender.create_adapter(mode='headless'))
    m._update(parent['id'],'director_observing','正在读取当前Working的受控观察。')
    from .director_request_scope import enabled
    result=cache.observe(parent,version,view,digest({'parent_id':parent['id'],'event_id':event['event_id'],'call_id':call['id']}),
                         admission_source='interactive' if enabled(runtime,parent) else 'autonomous')
    record=result['record']
    return {'observation':record,'result':{'ref':record['ref'],'identity':record['identity'],'observation':record['observation'],
            'cache_hit':result['cache_hit'],'renders':result['renders'],'bytes':result['bytes']}}

def metadata(runtime,parent,decision,call,body):
    from .director_goal_plan import apply
    from .scene_evidence import known_object_ids
    m=runtime.manager;store=runtime.store(parent['project_id'])
    with m.lock,m._projects().lock:
        receipt=store.metadata_receipt(parent['id'],decision,call,body)
        if receipt is None:
            m._assert_asset_write(parent['project_id'],run_id=parent['id'])
            if m._scene_workers_active(exclude=parent['id']):reject('活跃施工期间不能写Goal/Plan。','director_metadata_busy',409)
            project=m.get_project(parent['project_id']);current=project.get('current_scene')
            from .director_creation import authority
            if not current and not authority(runtime,parent):reject('元数据需要Working。','working_scene_missing',409)
            version=m.scene_version(current['run_id'],current['revision_id']) if current else None
            measured=json.loads(version['files']['inspection'].read_text(encoding='utf-8')) if version else {}
            known=known_object_ids(version['plan'],measured) if version else []
            if not version:
                from . import creative_continuation
                continuation=creative_continuation.current(runtime,parent)
                if continuation:
                    creative_continuation.verify(runtime,parent,continuation)
                    known=[r['id'] for r in continuation['objects']]+[r['instance_id'] for r in continuation['assets'].get('instances',[])]
            receipt=apply(store,parent,decision,call,body,known_ids=known)
            m.bind_refinement_goal(parent['id'])
        else:
            binding=store.state.refinement_state(parent['id'])['binding']
            if not binding or {k:binding['goal'][k] for k in ('id','version','content_digest')}!=receipt['goal_ref']:
                reject('原元数据绑定缺失，不能猜补。','director_metadata_unbound',409)
            parent.update(goal_snapshot=copy.deepcopy(binding['goal']),
                refinement_binding={k:copy.deepcopy(v) for k,v in binding.items() if k!='goal'},
                constraints=copy.deepcopy(binding['goal']['content']['constraints']))
    return {'metadata':receipt,'result':receipt}

def reply(runtime,parent,facts,body):
    if (not isinstance(body,dict) or set(body)-{'summary','next_focus','result_id','working_version'}
        or any(not isinstance(body.get(k),str) or not 1<=len(body[k])<=4000 for k in ('summary','next_focus'))):reject('Director回复字段无效。')
    result=facts.get('result')
    if result and (body.get('result_id')!=result['result_id'] or body.get('working_version')!=facts['working']['working_version']):
        from .runtime import AppError
        error=AppError('回复必须引用刚消费的真实结果及Working版本。','director_result_reference_invalid',409)
        error.reply_expected={'result_id':result['result_id'],'working_version':facts['working']['working_version']}
        raise error
    value={**body,'task_id':facts['task_id'],'working_version':facts['working']['working_version'],
           'input_image_sha256':[r['sha256'] for r in facts['working']['images']]}
    if runtime.is_readonly(parent):value.update(read_only=True,as_of=copy.deepcopy(facts.get('as_of')),source='model_readonly')
    return {'decision':value,'result':value}

def process(runtime,parent,event,decision,assistant,facts):
    from . import director_request_scope as scopes
    m=runtime.manager;store=runtime.store(parent['project_id']);calls=assistant.get('tool_calls') or []
    if decision.get('input',{}).get('report_pause_id'):
        if m._cancelled(parent['id']):return False
        if (not correction.known_result(store,parent,facts)
            or decision['input']['report_pause_id']!=(store.task().get('pause') or {}).get('pause_id')):
            reject('原收尾结果或安全暂停已变化，保留响应等待核实。','director_report_stale',409)
    if not calls:
        text=assistant.get('content')
        if not isinstance(text,str) or not text.strip():reject('完整响应没有文字或决策。','director_decision_missing',409)
        if correction.window(store,parent,facts):
            # Preserve the real assistant, but never fabricate the missing result reference.
            store.batch_call(event['event_id'],[])
            reference=runtime._private(parent).save(decision,assistant,'exchange',tools=[])
            store.apply_batch(parent['id'],event['event_id'],[assistant],[],reference,source='interactive')
            repair=correction.window(store,parent,facts)
            remaining=bool(repair and repair['remaining'])
            m._update(parent['id'],'director_reporting' if remaining else 'director_paused',
                '回复尚未引用原结果；施工暂停及原输出保留。',status='running' if remaining else 'awaiting_resume')
            return remaining
        body={'summary':text[:4000],'next_focus':'按用户后续输入继续'}
        if facts.get('result'):body.update(result_id=facts['result']['result_id'],working_version=facts['working']['working_version'])
        outcome=reply(runtime,parent,facts,body);outcome['decision'].update(source='model_text',action_taken=False)
        reference=runtime._private(parent).save(decision,assistant,'exchange',tools=[])
        store.apply_decision(parent['id'],event['event_id'],[assistant],decision=outcome['decision'],private_ref=reference)
        scopes.complete(runtime,parent);runtime._repair_reply(parent['id']);return False
    batch=store.batch_call(event['event_id'],calls)
    for index in range(batch['cursor'],len(calls)):
        call=calls[index];name=call['function']['name'];outcome={}
        prefix=batch['outcomes'];fresh=any(r.get('observation') or r.get('metadata') or r.get('scope_changed') for r in prefix)
        child=next((r.get('child_id') for r in prefix if r.get('child_id')),None)
        try:
            if correction.known_result(store,parent,facts) and name not in correction.REPLY_TOOLS:
                reject('施工仍暂停；本次只可汇报已知结果，不能观察、委派或申请资源。','director_report_only',409)
            if not scopes.enabled(runtime,parent) and runtime.is_readonly(parent) and name not in {'director_reply','director_clarify'}:
                reject('只读不能渲染或写入。','director_readonly_violation',409)
            if any(r.get('decision') for r in prefix):reject('本批已结束回复。','decision_already_completed',409)
            if child:reject('原子任务已准备，需先等待实际结果。','child_result_pending',409)
            if any(r.get('resource_request') for r in prefix):reject('未执行：本批已申请资源，完整配对后等待人类批准。','not_executed',409)
            scope_reply=name in {'director_reply','director_clarify'} and all(not r.get('observation') and not r.get('metadata') for r in prefix)
            if fresh and name!='director_observe' and not scope_reply:reject('新证据/范围必须先进入下一次推理。','requires_fresh_reasoning',409)
            if any(r.get('scope_changed') for r in prefix) and name=='director_observe':reject('先读取新范围回执再观察。','requires_fresh_reasoning',409)
            try:body=json.loads(call['function']['arguments'])
            except (ValueError,TypeError):reject('调用参数不是有效JSON对象；请以原工具schema纠正。','invalid_arguments_json')
            if not isinstance(body,dict):reject('调用参数必须为JSON对象。','invalid_arguments_structure')
            # Delegate reuses its original reservation before checking new-work
            # authority; a lost ACK must not strand the already prepared child.
            if name!='delegate_task':scopes.gate(runtime,parent,name,body)
            store.batch_call(event['event_id'],calls,index)
            if name=='director_observe':outcome=observe(runtime,parent,event,call,body)
            elif name=='director_goal_plan':outcome=metadata(runtime,parent,decision,call,body)
            elif name=='delegate_task':
                accepted=runtime._delegate(parent,call);outcome={'child_id':accepted['child_run_id'],'result':accepted}
            elif name=='director_request_scope':outcome={'scope_changed':True,'result':scopes.resolve(runtime,parent,body)}
            elif name=='request_resources':
                details=m._resource_host().director_request(runtime,parent,body)
                outcome={'resource_request':details,'result':{'requested':True,'approved':False,'same_run':parent['id']}}
            elif name in {'director_reply','director_clarify'}:outcome=reply(runtime,parent,facts,body)
            else:reject('未知Director工具。','director_invalid')
            outcome['tool']={'role':'tool','tool_call_id':call['id'],'content':json.dumps({'ok':True,'result':outcome['result']},ensure_ascii=False)}
            hook=getattr(runtime,'batch_checkpoint',None)
            if hook:hook('effect_returned',parent,call,outcome)
        except Exception as error:
            code=getattr(error,'code',None)
            # Unknown operation/private state is never turned into a retryable parameter error.
            if getattr(error,'outcome_unknown',False) or code in {'observation_outcome_unknown','director_continuation_unavailable','director_child_not_replayable'} or code is None:
                raise
            feedback={'code':code,'message':str(error)}
            if code=='director_result_reference_invalid':feedback['expected']=error.reply_expected
            if getattr(error,'details',{}).get('boundary')=='request_scope_validation':
                feedback['details']={**copy.deepcopy(error.details),'tool_call_id':call['id']}
            outcome={'tool':{'role':'tool','tool_call_id':call['id'],
                'content':json.dumps({'ok':False,'error':feedback},ensure_ascii=False)}}
        batch=store.batch_call(event['event_id'],calls,index,outcome=outcome)
        hook=getattr(runtime,'batch_checkpoint',None)
        if hook:hook('call_persisted',parent,call,outcome)
    outcomes=batch['outcomes'];tools=[r['tool'] for r in outcomes]
    reference=runtime._private(parent).save(decision,assistant,'exchange',tools=tools)
    store.apply_batch(parent['id'],event['event_id'],[assistant]+tools,outcomes,reference,
                      source='interactive' if scopes.enabled(runtime,parent) else 'autonomous')
    scope=scopes.current(runtime,parent)
    resource=next((r.get('resource_request') for r in outcomes if r.get('resource_request')),None)
    if resource:
        m._resource_host().seal_director(runtime,parent,[assistant]+tools,resource)
        return False
    child=next((r.get('child_id') for r in outcomes if r.get('child_id')),None)
    result=next((r.get('decision') for r in outcomes if r.get('decision')),None)
    if result:
        waiting=bool(scope and scope['state']=='awaiting_approval')
        scopes.complete(runtime,parent)
        runtime._repair_reply(parent['id'])
        if waiting:m._update(parent['id'],'director_awaiting_approval','持续范围提案已回复，等待人类原CAS批准。',status='awaiting_resume')
        elif scope and scope['state']=='queued':m._update(parent['id'],'director_queued','新任务已排队；空闲后按最新Working继续。',status='queued')
        return False
    if child:
        parent.update(status='waiting_child',stage='director_waiting',active_child_id=child);m._save(parent);return False
    if scope and scope['state'] in {'queued','awaiting_approval'}:
        m._update(parent['id'],'director_'+scope['state'],'请求已保留，等待空闲或原人类批准。',status='queued' if scope['state']=='queued' else 'awaiting_resume')
        return False
    if scopes.pause_applies(runtime,parent) and not runtime.is_readonly(parent):
        repair=correction.window(store,parent,facts)
        if repair and repair['remaining']:
            m._update(parent['id'],'director_reporting','施工暂停保持；正在纠正已有结果的回复引用。')
            return True
        m._update(parent['id'],'director_paused','本批完整配对已保留；原安全停止等待核实。',status='awaiting_resume')
        return False
    errors=[json.loads(r['tool']['content']).get('error',{}).get('code') for r in outcomes if not json.loads(r['tool']['content'])['ok']]
    session=store.session(parent['id']);signature=digest({'errors':errors,'working':facts['working']['working_version'],
        'scope':scope,'observations':[(r.get('observation') or {}).get('identity') for r in outcomes],
        'metadata':[(r.get('metadata') or {}).get('semantic_progress',{}).get('after',{}).get('digest') for r in outcomes]}) if errors else None
    if errors and set(errors)<={'director_stale','director_metadata_stale'}:
        from .director_goal_continuity import stale_signature
        signature=stale_signature(store,parent,scope)
    if signature and session.get('last_error_signature')==signature:
        session['error_repeats']=session.get('error_repeats',0)+1
    else:session['error_repeats']=0
    session['last_error_signature']=signature;store.save_session(session)
    if session['error_repeats']>=2:
        store.pause('repeated_tool_error_no_progress',{'error_codes':errors})
        m._update(parent['id'],'director_paused','相同工具错误持续且无进展，原配对已保留。',status='awaiting_resume');return False
    parent['stage']='director_planning';m._save(parent);return True
