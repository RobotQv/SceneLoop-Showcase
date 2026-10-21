"""SQL-bound natural user requests, finite work and explicit continuous approval."""
import copy
import json
import uuid
from .director_contract import digest,reject,tool,TOOLS,READONLY_TOOLS

SCOPE_TOOL=tool('director_request_scope',
    'Interpret the original accepted human message before acting. Discuss/inspect/clarify never authorize edits. '
    'Edit permits this one finite task. Continuous only proposes a scope; a separate protected human approval is required. '
    'basis MUST be an exact nonempty substring copied from the original human request, with NO explanatory prefix. '
    'stop_conditions MUST be null for discuss/inspect/edit/clarify; only continuous uses the exact two-field object. Never invent authority, budgets or permission.',
    {'intent':{'type':'string','enum':['discuss','inspect','edit','clarify','continuous']},
     'basis':{'type':'string','minLength':1,'maxLength':4000,'description':'Exact original human text substring only; do not prepend an explanation.'},
     'goal':{'type':'string','maxLength':4000,'description':'Required nonblank for edit/continuous.'},
     'criteria':{'type':'array','maxItems':16,'items':{'type':'string','minLength':1,'maxLength':1000},'description':'Nonempty for edit/continuous.'},
     'object_ids':{'type':'array','maxItems':32,'items':{'type':'string','minLength':1,'maxLength':1000},'description':'Existing object IDs permitted by the original request, not merely the initial focus; nonempty for edit/continuous.'},
     'focus_object_ids':{'type':'array','maxItems':16,'items':{'type':'string','minLength':1,'maxLength':1000},'description':'Optional initial focus, a subset of object_ids.'},
     'preserve':{'type':'string','maxLength':4000},
     'stop_conditions':{'type':['object','null'],'additionalProperties':False,'required':['total_saved_limit','stop_on_pass'],
        'description':'For every non-continuous intent, including a finite edit, send null. Only continuous sends exactly total_saved_limit and stop_on_pass; no other keys.',
        'properties':{'total_saved_limit':{'type':'integer','minimum':1,'maximum':100000,'description':'Absolute existing-task cumulative save limit, not an increment.'},
                      'stop_on_pass':{'type':'boolean'}}}},
    ['intent','basis','goal','criteria','object_ids','preserve','stop_conditions'])

def enabled(runtime,parent):
    store=runtime.stores.get(parent.get('project_id'))
    accepted=(store.session(parent['id']) or {}).get('run',{}) if store else {}
    if managed(runtime,parent):return True
    if accepted.get('creation') and (store.task() or {}).get('creative_authorization'):return False
    if parent.get('request_protocol')=='natural_v1':return True
    store=runtime.stores.get(parent.get('project_id'))
    return bool(store and (store.session(parent['id']) or {}).get('natural_recovery'))

def current(runtime,parent):return (runtime.store(parent['project_id']).session(parent['id']) or {}).get('request_scope')

def managed(runtime,parent):
    store=runtime.stores.get(parent.get('project_id'))
    session=store.session(parent['id']) if store else None
    return bool(session and (session['run'].get('request_protocol')=='natural_v2'
        and session['run'].get('execution_policy_version')==2 or session.get('natural_execution',{}).get('version')==2))

def interactive_execution(runtime,parent):
    scope=current(runtime,parent) or {}
    return bool(enabled(runtime,parent) and not pause_applies(runtime,parent)
        and (scope.get('intent')=='edit' or managed(runtime,parent) and scope.get('intent')=='continuous'))

def finite_authorization(accepted,scope):
    # Frozen only on a newly accepted human request; never backfill old Runs.
    return (accepted.get('request_protocol')=='natural_v1' and accepted.get('finite_scope_version')==1
            and (scope or {}).get('intent')=='edit')

def managed_authorization(accepted,scope,*,session=None,connection=None):
    if (scope or {}).get('intent') not in {'edit','continuous'}:return False
    if accepted.get('request_protocol')=='natural_v2' and accepted.get('execution_policy_version')==2:return True
    proof=(session or {}).get('natural_execution') or {}
    if (connection is None or accepted.get('request_protocol')!='natural_v1' or accepted.get('conversation_mode')=='read_only'
        or proof.get('version')!=2 or proof.get('source')!='protected_human_resume'
        or proof.get('accepted_request_digest')!=accepted.get('request_fingerprint')
        or (proof.get('original_scope') or {}).get('id')!=scope.get('id')):return False
    parent=accepted['id'];action_id=proof.get('action_id')
    if proof.get('event_id')!=digest({'parent':parent,'natural_execution_action':action_id}):return False
    row=connection.execute('SELECT document FROM director_actions WHERE action_id=?',(action_id,)).fetchone()
    event=connection.execute('SELECT parent_id,document FROM director_events WHERE event_id=?',(proof['event_id'],)).fetchone()
    binding=connection.execute('SELECT task_id FROM director_parents WHERE parent_id=?',(parent,)).fetchone()
    if not row or not event or not binding:return False
    receipt=json.loads(row[0])
    return (receipt.get('accepted') is True and receipt.get('execution_policy_version')==2
        and receipt.get('source')=='protected_human_resume' and receipt.get('action')=='resume'
        and receipt.get('action_id')==action_id and receipt.get('parent_id')==parent
        and receipt.get('project_id')==accepted.get('project_id') and receipt.get('task_id')==binding[0]
        and event[0]==parent and json.loads(event[1]).get('kind')=='natural_execution_resumed')

def new_finite(runtime,parent):
    if parent.get('request_protocol')!='natural_v1':return False
    session=runtime.store(parent['project_id']).session(parent['id'])
    return bool(session and finite_authorization(session['run'],session.get('request_scope')))

def pause_blocks(task,accepted,scope):
    pause=task.get('pause')
    if not pause:return False
    return not ((finite_authorization(accepted,scope) or accepted.get('execution_policy_version')==2 and (scope or {}).get('intent') in {'edit','continuous'}) and pause.get('scope')=='autonomous'
                and pause.get('reason')=='execution_authorization_exhausted')

def pause_applies(runtime,parent):
    store=runtime.store(parent['project_id']);task=store.task();pause=task.get('pause')
    if not pause:return False
    if parent.get('request_protocol') not in {'natural_v1','natural_v2'} or pause.get('scope')!='autonomous':return True
    session=store.session(parent['id'])
    if not session:return True
    if pause.get('reason') in {'decision_boundary_invalid','user_cancelled'}:
        return store.pause_blocks(task,session['run'],session.get('request_scope'))
    return pause_blocks(task,session['run'],session.get('request_scope'))

def busy(runtime,parent):
    m=runtime.manager
    return m._scene_workers_active(exclude=parent['id']) or any(
        r.get('project_id')==parent['project_id'] and r['id']!=parent['id']
        and r.get('operation')!='conversation' and r.get('status') not in {'completed','failed','interrupted','cancelled'}
        for r in m.runs.values())

def readonly_session(session):
    if session['run'].get('conversation_mode')=='read_only':return True
    is_managed=session['run'].get('execution_policy_version')==2 or session.get('natural_execution',{}).get('version')==2
    if session['run'].get('creation') and not is_managed:return False
    if session['run'].get('request_protocol') not in {'natural_v1','natural_v2'} and not session.get('natural_recovery') and not is_managed:return False
    scope=session.get('request_scope')
    return not scope or scope['intent'] not in {'edit','continuous'} or scope['state'] in {'queued','awaiting_approval','cancelled'}

def initial_writer(runtime,parent):
    """A queue may refer to a real initial writer without guessing its objects."""
    if not managed(runtime,parent) or runtime.manager.get_project(parent['project_id']).get('current_scene'):return None
    store=runtime.store(parent['project_id']);matches=[]
    for binding in store.bindings():
        child=runtime.manager.runs.get(binding['child_id'],{});worker=runtime.manager.workers.get(binding['child_id'])
        if (binding['task_id']==parent['director_task_id'] and binding['parent_id']!=parent['id']
                and binding['state'] in {'started','waiting'} and not binding.get('result')
                and child.get('project_id')==parent['project_id'] and child.get('bootstrap')
                and child.get('status')=='running' and worker and worker.is_alive()
                and not child.get('error') and not child.get('cancel_requested')):matches.append(binding['delegation_id'])
    return matches[0] if len(matches)==1 else None

def resolve(runtime,parent,body):
    if (not isinstance(body,dict) or not {'intent','basis','goal','criteria','object_ids','preserve','stop_conditions'}<=set(body)
        or set(body)-{'intent','basis','goal','criteria','object_ids','focus_object_ids','preserve','stop_conditions'}):
        missing=sorted({'intent','basis','goal','criteria','object_ids','preserve','stop_conditions'}-set(body)) if isinstance(body,dict) else ['body']
        scope_error('范围解释字段不完整。',missing)
    intent=body['intent']
    if intent not in ('discuss','inspect','edit','clarify','continuous'):reject('未知请求意图。','director_scope_invalid')
    accepted=runtime.accepted_request(parent)
    is_managed=managed(runtime,parent)
    fresh_creation=is_managed and bool(accepted.get('creation')) and not runtime.manager.get_project(parent['project_id']).get('current_scene')
    writer=initial_writer(runtime,parent) if intent in {'edit','continuous'} else None
    for key in ('basis','goal','preserve'):
        if not isinstance(body[key],str) or len(body[key])>4000:reject(key+': 必须为最多4000字的字符串。','director_scope_invalid')
    if not body['basis'].strip() or body['basis'] not in accepted['prompt']:reject('basis: 必须逐字复制原用户请求中的非空子串；不要加“根据用户要求”等解释前缀。','director_scope_invalid')
    for key in ('criteria','object_ids'):
        if not isinstance(body[key],list) or len(body[key])>(32 if key=='object_ids' else 16) or any(not isinstance(v,str) or not v or len(v)>1000 for v in body[key]):
            reject(key+': 必须为有界字符串列表；criteria最多16项，object_ids最多32项，每项1–1000字。','director_scope_invalid')
    if intent in {'edit','continuous'} and (not body['goal'].strip() or not body['criteria'] or not body['object_ids'] and not (fresh_creation or writer)):
        reject('施工范围需要目标、标准和实际对象。','director_scope_invalid')
    if accepted.get('conversation_mode')=='read_only' and intent in {'edit','continuous'}:
        reject('显式只读不能施工。','director_readonly_violation',409)
    store=runtime.store(parent['project_id']);session=store.session(parent['id']);old=session.get('request_scope')
    if old:
        if old['interpretation_digest']!=digest(body):reject('本次范围已冻结；意图升级需要新用户请求。','director_scope_frozen',409)
        return old
    from .observation_service import working
    view,_=working(runtime.manager,parent['project_id']);known={r['id'] for r in view['objects']}
    known.update(row['instance_id'] for row in view.get('assets',{}).get('instances',[]))
    from . import creative_continuation
    continuation=creative_continuation.current(runtime,parent)
    if continuation and (not view['selection'] or continuation.get('kind')=='pending_edit') and not writer:
        if intent in {'edit','continuous'}:creative_continuation.verify(runtime,parent,continuation)
        known={row['id'] for row in continuation.get('objects',[])}
        known.update(row['instance_id'] for row in continuation.get('assets',{}).get('instances',[]))
    if set(body['object_ids'])-known:
        scope_error('object_ids 只能使用当前候选/Working的物体或资产实例 ID；项目、任务、参考图 ID 不是物体。',
                    ['object_ids'],available_object_ids=sorted(known),source=continuation['source'] if continuation else view['selection'])
    focus=body.get('focus_object_ids',[])
    if (not isinstance(focus,list) or len(focus)>16 or any(not isinstance(v,str) for v in focus)
            or set(focus)-set(body['object_ids'])):reject('初始焦点须在已解释的用户范围内。','director_scope_invalid')
    stop=body['stop_conditions']
    if intent=='continuous':
        if (not isinstance(stop,dict) or set(stop)!={'total_saved_limit','stop_on_pass'}
            or type(stop['total_saved_limit']) is not int or not 1<=stop['total_saved_limit']<=100000
            or type(stop['stop_on_pass']) is not bool):reject('stop_conditions: continuous必须为且仅为{total_saved_limit: 1–100000整数, stop_on_pass: 布尔值}；上限为绝对累计值。','director_scope_invalid')
    elif stop is not None:reject('stop_conditions: intent='+intent+'必须传null；只有continuous可传持续停止条件对象。','director_scope_invalid')
    identifier=uuid.uuid5(uuid.UUID(parent['id']),'request-scope').hex
    scope={'id':identifier,'request_digest':digest({'parent_id':parent['id'],'prompt':accepted['prompt']}),
           'source':'accepted_human_request_interpreted','interpretation_digest':digest(body),**copy.deepcopy(body),
           'state':'awaiting_approval' if intent=='continuous' and not is_managed else 'queued' if intent in {'edit','continuous'} and busy(runtime,parent) else 'classified',
           'max_children':None if is_managed and intent in {'edit','continuous'} else 1 if intent=='edit' else 0,
           'baseline':view['selection'],'working_version':view['working_version']}
    if continuation and not writer:
        scope['creative_continuation']=copy.deepcopy(continuation)
        if continuation['mode']=='same_run' and (intent=='edit' or is_managed and intent=='continuous'):scope['state']='bound_existing'
    if intent=='continuous' and not is_managed:scope.update(proposal_id=uuid.uuid5(uuid.UUID(parent['id']),'continuous-proposal').hex,
        scope_digest=digest({'request':scope['request_digest'],'interpretation':body}))
    session['request_scope']=scope
    if (session.get('queue') or {}).get('interpretation_required'):
        session['queue']['interpretation_required']=False
        session['queue']['rebound_scope_id']=scope['id']
    if scope['state']=='queued':session['queue']={'state':'accepted','reason':'project_writer_active','accepted_at':parent['created_at'],
                                                 'rebinding_required':True,
        **({'reinterpret_at_idle':True,'source_delegation_id':writer} if writer else {})}
    store.save_session(session)
    if continuation:creative_continuation.recover_scope_pause(runtime,parent,scope)
    return scope

def scope_error(message,fields,**facts):
    from .runtime import AppError
    error=AppError(message,'director_scope_invalid',400)
    error.details={'execution':'confirmed_not_executed','boundary':'request_scope_validation','fields':fields,**facts}
    raise error

def can_dispatch(runtime,parent):
    store=runtime.store(parent['project_id']);scope=current(runtime,parent)
    if not scope or scope['intent'] not in {'edit','continuous'}:return False
    if scope['state'] in {'queued','awaiting_approval','completed','cancelled','bound_existing'} or pause_applies(runtime,parent):return False
    if not new_finite(runtime,parent):
        try:store.execution_admission(1 if managed(runtime,parent) else parent['builder_iteration']['max_rounds'],requestable=True)
        except Exception as error:
            if getattr(error,'code',None) in {'director_execution_pool_exhausted','director_execution_pool_unknown'}:return False
            raise
    if managed(runtime,parent):return stopping_reason(runtime,parent) is None
    if scope['intent']=='edit':return not store.bindings(parent['id'])
    task=store.task();grant=task.get('authorization',{}).get('continuous')
    if not task['policy']['autonomous'] or not grant or grant['scope_digest']!=scope['scope_digest']:return False
    if scope['stop_conditions']['stop_on_pass']:
        if any(r.get('result') and any(e.get('verdict')=='pass' and e.get('application_status')=='applied' for e in r['result']['evaluations'])
               for r in store.bindings(parent['id'])):return False
    return True

def stopping_reason(runtime,parent):
    if not managed(runtime,parent):return None
    stop=(current(runtime,parent) or {}).get('stop_conditions')
    if not stop:return None
    store=runtime.store(parent['project_id'])
    if store.execution_used()>=stop['total_saved_limit']:return 'request_save_limit_reached'
    if stop['stop_on_pass'] and any(r.get('result') and any(e.get('verdict')=='pass' and e.get('application_status')=='applied'
        for e in r['result']['evaluations']) for r in store.bindings(parent['id'])):return 'request_quality_stop_reached'
    return None

def execution_saved_target(runtime,parent):
    store=runtime.store(parent['project_id']);caps=[parent['builder_iteration']['max_rounds']]
    remaining=store.execution_remaining()
    if remaining is not None:caps.append(remaining)
    stop=(current(runtime,parent) or {}).get('stop_conditions')
    if stop:caps.append(max(0,stop['total_saved_limit']-store.execution_used()))
    return min(caps)

def allowed_objects(runtime,parent,scope):
    """New objects derive from this request's own published construction only."""
    allowed=set(scope['object_ids'])
    if not managed(runtime,parent):return allowed
    from .director_creation import authority
    creative=authority(runtime,parent)
    if not creative or not creative['approved']['creation']['allow_new_objects']:return allowed
    store=runtime.store(parent['project_id']);m=runtime.manager
    with store.state._connection() as c:
        bindings=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_delegations WHERE parent_id=? ORDER BY rowid',(parent['id'],))]
    completed=[r for r in bindings if r['state']=='completed' and r.get('result')]
    if not completed:return allowed
    project=m.get_project(parent['project_id']);last=completed[-1]['result']['working']
    if (last['selection']!=project.get('current_scene') or last['working_version']!=project['workspace']['working_version']):
        reject('当前Working不再属于本请求的已确认施工链。','director_scope_stale',409)
    for record in completed:
        result=record['result'];selection=result['working']['selection']
        if not selection or selection['run_id']!=record['child_id']:continue
        receipts=store.state.receipts_for_run(record['child_id'])
        if not any(r['working_version']==result['working']['working_version'] for r in receipts):
            reject('对象范围演进缺少正式Working回执。','director_scope_stale',409)
        version=m.scene_version(selection['run_id'],selection['revision_id'])
        ids={r['id'] for r in version['plan'].get('objects',[])}
        ids.update(r['instance_id'] for r in version['plan'].get('assets',{}).get('instances',[]))
        baseline={r['id'] for r in record['brief']['baseline']['objects']}
        baseline.update(r['instance_id'] for r in record['brief']['baseline'].get('assets',{}).get('instances',[]))
        allowed.update(ids-baseline)
    return allowed

def gate(runtime,parent,name,body):
    if not enabled(runtime,parent):return
    scope=current(runtime,parent);store=runtime.store(parent['project_id'])
    if name in {'director_reply','director_clarify'}:return
    if name=='director_request_scope':return
    if parent.get('conversation_mode')=='read_only':reject('显式只读不能渲染或写入。','director_readonly_violation',409)
    if not scope:
        # Controlled recovery may consume already-confirmed legacy read-only observations.
        if store.session(parent['id']).get('natural_recovery') and name=='director_observe':return
        reject('先解释本次原用户请求，不能先施工或观察。','director_scope_required',409)
    if name=='request_resources':
        if stopping_reason(runtime,parent):reject('本请求已达到停止条件；追加资源不能扩大当前目标停止边界，请报告实际结果。','director_request_stopped',409)
        if scope['intent'] not in {'edit','continuous'} or pause_applies(runtime,parent):
            reject('当前范围不允许申请执行资源。','director_scope_denied',409)
        return
    if name=='director_observe':
        if scope['intent'] not in {'inspect','edit','continuous'}:reject('分析/澄清范围未请求观察。','director_scope_denied',409)
        if pause_applies(runtime,parent):reject('原暂停未核实，不能新增观察。','director_autonomy_paused',409)
        if busy(runtime,parent):reject('施工忙时只读已有证据，不能并发渲染。','project_writer_active',409)
        return
    if not can_dispatch(runtime,parent):reject('本次范围不允许新增施工。','director_scope_denied',409)
    permitted=allowed_objects(runtime,parent,scope)
    if name=='delegate_task':
        from .director_contract import delegation
        value=delegation(body,allow_empty_objects=bool(scope.get('creative_continuation') or runtime.accepted_request(parent).get('creation')))
        if set(value['object_ids'])-permitted:reject('委派超出本次冻结对象范围。','director_scope_denied',409)
    if name=='director_goal_plan':
        from .director_goal_plan import validate
        changes=validate(body,'0'*64,allow_empty_baseline=bool(scope.get('creative_continuation') or runtime.accepted_request(parent).get('creation'))).get('changes',[])
        if any(set(row.get('set',{}).get('object_ids',[]))-permitted for row in changes):
            reject('Plan超出本次冻结对象范围。','director_scope_denied',409)

def tools(runtime,parent):
    scope=current(runtime,parent);reply=[copy.deepcopy(q) for q in READONLY_TOOLS]
    if not scope:
        interpretation=copy.deepcopy(SCOPE_TOOL)
        if managed(runtime,parent):
            interpretation['function']['description']='Interpret the accepted human request before acting. Discuss/inspect/clarify do not edit. Edit and continuous are internal plans within this same goal and existing resource limits; neither needs another scope approval. Respect explicit do-not-modify and preservation instructions. basis is an exact original request substring. New empty creation may use object_ids=[]. While a real initial Builder is active and Working is empty, queue new work with object_ids=[]; never guess future IDs. stop_conditions is null except continuous uses total_saved_limit and stop_on_pass; it never increases the actual resource budget.'
        return [interpretation]+reply
    if parent.get('conversation_mode')=='read_only' or busy(runtime,parent):return reply
    if pause_applies(runtime,parent):return reply
    if scope['state'] in {'queued','awaiting_approval','cancelled','completed'}:return reply
    if scope['intent'] in {'discuss','clarify'}:return reply
    if scope['intent']=='inspect' or not can_dispatch(runtime,parent):
        return reply+[copy.deepcopy(q) for q in TOOLS if q['function']['name']=='director_observe']
    from .director_creation import tools as creation_tools
    return creation_tools(runtime,parent)+[copy.deepcopy(READONLY_TOOLS[1])]

def complete(runtime,parent):
    if not enabled(runtime,parent):return
    store=runtime.store(parent['project_id']);session=store.session(parent['id']);scope=session.get('request_scope')
    if scope and scope['state'] not in {'awaiting_approval','queued'}:scope['state']='completed';store.save_session(session)

def natural_resume_proof(runtime,parent):
    """Read-only qualification of one legacy scope-only request, never an upgrade."""
    store=runtime.store(parent['project_id']);session=store.session(parent['id']);scope=(session or {}).get('request_scope') or {}
    accepted=(session or {}).get('run',{})
    if (accepted.get('request_protocol')!='natural_v1' or accepted.get('conversation_mode')=='read_only'
            or scope.get('intent')!='continuous' or scope.get('state')!='awaiting_approval'
            or session.get('natural_execution') or parent.get('cancel_requested') or parent.get('error')
            or parent.get('director_ledger_unknown') or store.bindings(parent['id'])):
        reject('此请求不属于可核验的旧范围等待。','director_natural_resume_ineligible',409)
    if busy(runtime,parent):reject('原项目仍有活跃执行，保留等待。','project_writer_active',409)
    runtime._native_exchange(parent,session)
    from .observation_service import working
    view,_=working(runtime.manager,parent['project_id'])
    if view['selection']!=scope['baseline'] or view['working_version']!=scope['working_version']:
        reject('原请求候选或Working已变化。','director_stale',409)
    from . import creative_continuation
    continuation=scope.get('creative_continuation')
    if continuation:creative_continuation.verify(runtime,parent,continuation)
    pause=store.task().get('pause');pause_proof=None
    if pause:
        pause_proof=creative_continuation.recover_scope_pause(runtime,parent,scope,prove_only=True)
        if not pause_proof:reject('原暂停不是已证明的纯范围参数拒绝。','director_legacy_pause_unverified',409)
    with store.state._connection() as c:
        if (c.execute('SELECT 1 FROM director_metadata WHERE parent_id=?',(parent['id'],)).fetchone()
                or c.execute('SELECT 1 FROM operations WHERE run_id=?',(parent['id'],)).fetchone()
                or c.execute('SELECT 1 FROM receipts WHERE run_id=?',(parent['id'],)).fetchone()):
            reject('本父已有施工动作，不能按零施工迁移。','director_natural_resume_ineligible',409)
        attempts=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_attempts WHERE task_id=?',(store.task()['task_id'],))]
        decisions=[json.loads(r[0]) for r in c.execute('SELECT document FROM director_decisions')]
        if (any(r['state']!='settled' or r.get('cost_conflict') or r.get('cost_anomaly') for r in attempts)
                or any(r['state'] in {'started','unknown'} for r in decisions)
                or c.execute("SELECT 1 FROM operations WHERE state='unknown'").fetchone()
                or c.execute("SELECT 1 FROM edit_attempts WHERE status IN ('scheduled','unknown')").fetchone()
                or c.execute('SELECT 1 FROM reconciliation').fetchone()
                or c.execute('SELECT 1 FROM run_counters WHERE needs_reconciliation!=0').fetchone()
                or c.execute('SELECT 1 FROM asset_run_requests WHERE closed=0').fetchone()
                or c.execute("SELECT 1 FROM local_rounds WHERE status IN ('active','paused')").fetchone()):
            reject('原调用、费用或写入尚未核实，不能迁移。','director_unknown_requires_reconciliation',409)
        if any(r['state']!='completed' for r in [json.loads(q[0]) for q in c.execute('SELECT document FROM director_delegations')]):
            reject('另有未完成委派，不能迁移本请求。','director_natural_resume_ineligible',409)
        for row in c.execute('SELECT document FROM asset_generation_jobs'):
            job=json.loads(row[0])
            cost_known=job['cost'] in {'consumed','released','no_charge_confirmed'}
            if not cost_known:
                from .asset_local_authorization import confirmed_local_resource_job,confirmed_local_resource_failure
                request=c.execute('SELECT document FROM asset_run_requests WHERE job_id=?',(job['job_id'],)).fetchone()
                if request:
                    doc=json.loads(request[0])
                    cost_known=confirmed_local_resource_job(store.state,doc,job) or confirmed_local_resource_failure(store.state,doc,job)
            if job['dispatch'] in {'submitting','outcome_unknown'} or job['remote'] not in {'succeeded','failed','banned','expired','cancelled'} or not cost_known:
                reject('原生成或费用尚未确定。','director_unknown_requires_reconciliation',409)
    return {'version':2,'session_digest':digest(session),'accepted_request_digest':accepted['request_fingerprint'],
        'original_scope':copy.deepcopy(scope),'private_ref':copy.deepcopy(session['private_ref']),'pause_proof':pause_proof}

def natural_resume_view(runtime,parent):
    from .runtime import AppError
    try:natural_resume_proof(runtime,parent)
    except AppError as error:return {'version':2,'can_continue':False,'reason':error.code}
    return {'version':2,'can_continue':True,'reason':None}

def cancel_queued(runtime,parent):
    if not enabled(runtime,parent):return False
    store=runtime.store(parent['project_id']);session=store.session(parent['id'])
    state=(session.get('queue') or {}).get('state')
    if state not in {'accepted','dispatching'}:return False
    if state=='dispatching':
        decision=store.decision_for(session['event_id']);worker=runtime.manager.workers.get(parent['id'])
        if decision and decision['state']!='prepared' or worker and worker.is_alive():return False
    session['queue']['state']='cancelled'
    if session.get('request_scope'):session['request_scope']['state']='cancelled'
    store.save_session(session)
    runtime.manager.record_conversation_turn(parent['id'],kind='director',summary='已取消本排队请求，其他活跃任务保持。',status='cancelled')
    return True

def schedule(runtime,project_id,*,exclude=None):
    m=runtime.manager;store=runtime.store(project_id)
    if m._scene_workers_active(exclude=exclude):return False
    if any(r['id']!=exclude and r.get('project_id')==project_id and r.get('status') in {'running','waiting_child'}
           and not runtime.is_readonly(r) for r in m.runs.values() if r.get('operation')=='conversation'):return False
    queued=sorted([s for s in store.sessions() if (s.get('queue') or {}).get('state')=='accepted'],key=lambda s:s['run']['created_at'])
    for session in queued:
        parent=m.runs.get(session['parent_id'])
        if not parent or parent.get('cancel_requested'):continue
        if any(r.get('project_id')==project_id and r.get('operation')!='conversation' and r['id']!=exclude
               and r.get('status') not in {'completed','failed','interrupted','cancelled'} for r in m.runs.values()):return False
        project=m.get_project(project_id)
        store.activate_queue(parent['id'],project)
        parent.update(status='running',stage='director_rebinding',expected_project_revision=project['revision'],error=None)
        m._save(parent);m._start(parent,runtime.execute);return True
    return False

def approved(runtime,parent,receipt):
    if not enabled(runtime,parent):return False
    store=runtime.store(parent['project_id']);session=store.session(parent['id']);scope=session.get('request_scope')
    grant=store.task().get('authorization',{}).get('continuous')
    if (not scope or scope['intent']!='continuous' or not grant or store.task().get('pause')
        or not store.task()['policy']['autonomous'] or grant.get('id')!=scope['id']
        or grant.get('scope_digest')!=scope['scope_digest']):return False
    action_id=receipt['action_id']
    if receipt.get('source')=='protected_human_recover':
        # Reconcile a committed human grant after a crash before its event/session.
        # Recovery supplies no new authority and cannot substitute another proposal.
        action_id=grant['action_id']
        with store.state._connection() as c:
            row=c.execute('SELECT document FROM director_actions WHERE action_id=?',(action_id,)).fetchone()
        original=json.loads(row[0]) if row else {}
        if (original.get('source')!='protected_human_policy' or not original.get('policy',{}).get('autonomous')
            or original.get('policy',{}).get('continuous_scope')!={k:scope[k] for k in ('proposal_id','scope_digest')}):
            reject('原持续批准回执不可核验，不能补发授权。','director_scope_invalid',409)
    elif receipt.get('source')!='protected_human_policy':return False
    event_id=digest({'parent':parent['id'],'approved_action':action_id})
    if session.get('approval_event_id')==event_id:return False
    scope.update(state='active',max_children=None)
    with store.state._write() as c:
        c.execute('INSERT OR IGNORE INTO director_events VALUES (?,?,?,0)',(event_id,parent['id'],
            json.dumps({'kind':'continuous_approved','source':'interactive','state':'prepared','result':session.get('result')})))
    session.update(event_id=event_id,approval_event_id=event_id,decision=None,turn_pending=False,state='scope_approved')
    store.save_session(session);return True

def view(runtime,parent):
    if not enabled(runtime,parent):return {}
    store=runtime.store(parent['project_id']);session=store.session(parent['id']);scope=session.get('request_scope');proposal=None
    if scope and scope['intent']=='continuous' and not managed(runtime,parent):
        proposal={k:copy.deepcopy(scope[k]) for k in ('proposal_id','scope_digest','goal','criteria','object_ids','preserve','stop_conditions')}
        proposal['source_request_id']=parent['id']
    with store.state._connection() as c:
        used=c.execute('SELECT COALESCE(SUM(r.saved_edit_rounds),0) FROM run_counters r JOIN director_delegations d ON d.child_id=r.run_id').fetchone()[0]
    grant=store.task().get('authorization',{}).get('continuous');queue=copy.deepcopy(session.get('queue'))
    if queue:
        pending=sorted([s for s in store.sessions() if (s.get('queue') or {}).get('state')=='accepted'],key=lambda s:s['run']['created_at'])
        queue['position']=next((i+1 for i,s in enumerate(pending) if s['parent_id']==parent['id']),0)
    phase='interpreting' if not scope else 'queued' if scope['state']=='queued' else 'needs_input' if scope['state']=='awaiting_approval' else (
        'completed' if parent['status']=='completed' else 'building' if parent['status']=='waiting_child' else 'planning')
    if parent.get('active_child_id'):
        child=runtime.manager.runs.get(parent['active_child_id'],{})
        if (child.get('asset_request') or {}).get('phase')=='approval':phase='asset_approval'
        elif 'judge' in child.get('stage',''):phase='judging'
    if parent.get('stage')=='director_observing' and parent['status']=='running':phase='observing'
    blocked=pause_applies(runtime,parent)
    task=store.task();blocking=None
    if blocked and (task.get('pause') or {}).get('reason')=='user_cancelled':
        with store.state._connection() as c:blocking=store.finite_stop_blocker(c,task,parent['id'])
    if parent['status'] in {'completed','failed','cancelled','interrupted'}:phase=parent['status']
    elif parent['status']=='awaiting_resume':phase='paused'
    elif blocked and scope and scope['intent']=='edit' and phase=='planning':phase='paused'
    summary={'interpreting':'正在理解本次请求；尚未正式回复','queued':'新任务已排队，等待空闲后继续',
        'needs_input':'持续范围已提出，等待明确批准','planning':'已理解本次范围，正在准备后续步骤；尚未正式回复',
        'observing':'正在读取当前Working的受控观察；尚未正式回复','building':'正在等待Builder实际执行结果',
        'asset_approval':'正在等待资产批准','judging':'正在等待独立评审结果','completed':'本次请求已回复',
        'paused':'任务已暂停，等待核实原请求或执行回执','failed':'本次请求失败，请核实已保存的执行回执',
        'cancelled':'本次请求已取消','interrupted':'本次请求已中断，保留原执行记录'}[phase]
    if phase=='paused' and blocking:summary=blocking['reason']
    return {'request_scope':copy.deepcopy(scope),'authorization':{'can_dispatch':can_dispatch(runtime,parent),'pause_applies':blocked,
        'execution_policy_version':2 if managed(runtime,parent) else 1,
        'scope_approval_required':bool(scope and scope['state']=='awaiting_approval' and not managed(runtime,parent)),
        'runtime_access':'blocked' if blocked else 'interpreting' if not scope else 'queued' if scope['state']=='queued' else 'read_only' if readonly_session(session) else 'executing',
        'blocked_reason':(store.task().get('pause') or {}).get('reason') if blocked else None,
        'request_stop_reason':stopping_reason(runtime,parent),'continuous_proposal':proposal,'continuous':grant,'used_saved_rounds':used,
        **({'blocking':blocking} if blocking else {})},
        'queue':queue,'activity':{'phase':phase,'summary':summary,
            'safe_to_cancel':True,'current_child_id':parent.get('active_child_id')}}
