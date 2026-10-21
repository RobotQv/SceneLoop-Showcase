"""M1 event-driven Director parent and existing single-writer Builder handoff."""
import copy
import json
import time
import uuid
from . import director_context
from .director_contract import TOOLS, READONLY_TOOLS, delegation, digest, normalize, reject
from .director_store import DirectorStore
from .director_continuation import Continuation
from .observation_service import working

class DirectorRuntime:
    def __init__(self, manager):
        self.manager=manager
        self.stores={}
        self._repairing_turns=set()
    def store(self, project_id):
        if project_id not in self.stores:
            self.stores[project_id]=DirectorStore(self.manager._projects().state_store(project_id),
                cost_source=getattr(self.manager,'director_cost_source',None),mock_provider=self.manager.providers)
        # Internal backend injection only; hydration/GET never starts a quote or
        # receipt request. A fresh process must explicitly supply its authority.
        self.stores[project_id].cost_source=getattr(self.manager,'director_cost_source',None)
        self.stores[project_id].run_states=self.manager.runs
        self.stores[project_id].run_directories=self.manager.run_directories
        self.stores[project_id].worker_states=self.manager.workers
        return self.stores[project_id]
    def cost_summary(self, project_id, task_id):
        # Context-only inspection must not create/register a Director ledger.
        store=self.stores.get(project_id)
        if store is None:
            from .director_ledger import unbound_summary
            return unbound_summary(task_id)
        return store.cost_summary(task_id)
    def accepted_request(self, parent):
        store=self.stores.get(parent['project_id'])
        session=store.session(parent['id']) if store else None
        return session['run'] if session else parent
    def is_readonly(self, parent):
        store=self.stores.get(parent.get('project_id'))
        return bool(store and store.readonly(parent['id']))
    def binding(self, run_id, project_id):
        return self.store(project_id).child(run_id) if project_id else None
    def write_blocked(self, project_id, run_id=None):
        if project_id not in self.stores: return False
        return any(record['state'] in {'reserved','prepared','started','waiting'} and record['child_id']!=run_id
                   for record in self.stores[project_id].bindings())
    def create(self, value):
        from .runtime import now
        m=self.manager; request=normalize(value); fingerprint=digest(request)
        with m.lock:
            for run in m.runs.values():
                if run['idempotency_key']==request['idempotency_key']:
                    if run['request_fingerprint']!=fingerprint: reject('提交身份已用于不同请求。','idempotency_conflict',409)
                    return m.get(run['id'])
            existing=self.store(request['project_id']).parent_by_key(request['idempotency_key'])
            if existing:
                if existing['digest']!=fingerprint: reject('提交身份内容冲突。','idempotency_conflict',409)
                self.hydrate()
                return m.get(existing['parent_id'])
            managed=request.get('request_protocol')=='natural_v2'
            natural=managed or request.get('request_protocol')=='natural_v1' and not request.get('creation')
            if natural and request['director_policy'] is None:
                task=self.store(request['project_id']).task()
                request['director_policy']=copy.deepcopy(task['policy'] if task else {'mode':'statistics_only','autonomous':False})
                if managed and not task:request['director_policy']['total_saved_limit']=request['iteration']['max_rounds']
            readonly=natural or request.get('conversation_mode')=='read_only'
            if any(r.get('operation')=='conversation' and
                   ((readonly and r.get('project_id')==request['project_id'] and self.is_readonly(r) and
                     (m.workers.get(r['id']) and m.workers[r['id']].is_alive())) or
                    (not readonly and not self.is_readonly(r) and r['status'] not in {'completed','failed','interrupted','cancelled','awaiting_resume'}))
                   for r in m.runs.values()): reject('当前同类Director请求仍在处理。','busy',409)
            project=m.get_project(request['project_id'])
            if project['revision']!=request['expected_project_revision']: reject('项目修订已变。','project_changed',409)
            from .director_creation import freeze_request
            freeze_request(m,request)
            for profile in ([request['profiles']['brain']] if readonly else request['profiles'].values()): m.providers.validate_profile(m._execution_profile(profile),'brain')
            if request['judge'].get('mode')=='profile':
                from .runtime import _normalize_request_profiles
                configured=request['judge']['profile']
                repository=m.profile_repository
                saved=next((p for p in repository.list() if p['id']==configured['id']),None) if repository else None
                if saved is None:reject('独立Judge配置未保存或配置库不可用。','configuration_required')
                if saved['revision']!=configured['revision']:reject('独立Judge配置修订已变。','revision_conflict',409)
                if _normalize_request_profiles({'profiles':{'brain':saved}},'edit')['brain']!=configured:
                    reject('独立Judge配置与保存记录不一致。','profile_snapshot_mismatch',409)
            if request['judge'].get('mode')=='profile' or request['judge'].get('reasoning_effort') is not None:
                m.providers.validate_profile(m._judge_profile({**request,'profiles':{'brain':request['profiles']['builder']}}),'brain')
            if request.get('asset_generation'):
                host=m._asset_host()
                profile=next((row for row in host.profiles.list() if row['id']==request['asset_generation']['profile_id']),None)
                if request.get('local_profile_snapshot'):
                    supported=bool(profile and request.get('creation') and profile==request.get('local_profile_snapshot')
                        and profile['id']==request.get('local_resources',{}).get('profile_id',profile['id']) and profile.get('mode')=='local_process'
                        and profile.get('provider_id')=='hunyuan' and profile.get('model')=='hunyuan3d-2mini')
                else:
                    supported=bool(profile and profile['provider_id']=='tripo' and profile['model']=='v3.1-20260211')
                if not supported:
                    reject('资产配置不存在或不属于现有静态shape合同。','asset_configuration_conflict',409)
            rid=uuid.uuid4().hex
            run={**request,'id':rid,'request_fingerprint':fingerprint,'created_at':now(),
                 'status':'running','stage':'director','events':[],'artifacts':[],'cancel_requested':False,
                 'usage':{'director_calls':0,'brain_calls':0,'judge_calls':0,'image_calls':0,'asset3d_calls':0,'retries':0,'cost':None}}
            run['builder_iteration']=run['iteration']
            if natural and not managed:run['finite_scope_version']=1
            if managed:run['execution_policy_version']=2
            run['iteration']={'enabled':False,'max_rounds':1}
            if readonly:run['readonly_accepted']={'at':run['created_at'],'project_id':project['id'],
                'working':{'selection':copy.deepcopy(project.get('current_scene')),
                           'working_version':project['workspace']['working_version']}}
            task=self.store(project['id']).register(run,fingerprint)
            run['director_task_id']=task['task_id']
            from .agent_resources import freeze as freeze_resources
            freeze_resources(run, self.manager._projects().state_store(run['project_id']))
            m.runs[rid]=run; m.run_directories[rid]=m._projects().runs_root(project['id'])/rid
            if request.get('creation'):
                from .director_creation import select_stage
                select_stage(self,run)
            if readonly:m.record_conversation_turn(rid,kind='director',summary='',status='running')
            m._save(run); m._start(run,self.execute)
            return m.get(rid)
    def detail(self, run):
        store=self.store(run['project_id'])
        if not store.parent(run['id']): reject('父请求SQL关联缺失。','director_unbound',409)
        session=store.session(run['id'])
        if store.readonly(run['id']):
            if session.get('turn_pending') and session.get('decision') and run['id'] not in self._repairing_turns:
                self._repair_reply(run['id'])
        from .director_readonly import asset_facts
        from .director_request_scope import view,natural_resume_view
        from .director_timeline import page
        detail=store.detail(run['id'])
        if (session.get('request_scope') or {}).get('state')=='awaiting_approval':
            detail['recovery']['natural_execution']=natural_resume_view(self,run)
        return {**detail,'assets':asset_facts(self.manager,run['project_id'],store.task()['task_id']),
                **view(self,run),'timeline':page(store,run['id'],self.manager.runs)}
    def _private(self, parent):
        return Continuation(self.manager.run_directory(parent['id']),parent['project_id'],parent['id'])
    def _native_exchange(self, parent, session):
        if not session['exchange']:
            if session['state']=='prepared': return []
            reject('原Provider续接包缺失，不能用新上下文重发。','director_continuation_unavailable',409)
        store=self.store(parent['project_id']); reference=session.get('private_ref')
        decision=store.decision_for((reference or {}).get('event_id'))
        if not decision or decision['state']!='applied' or (reference or {}).get('slot')!='exchange':
            reject('原Provider续接claim缺失，旧裁剪投影不能重建私有状态。','director_continuation_unavailable',409)
        assistant=self._private(parent).load(reference,decision,tools=session['exchange'][1:])
        from .director_continuation import public_assistant
        if public_assistant(assistant)!=session['exchange'][0]:
            reject('原Provider续接配对投影已变化。','director_continuation_unavailable',409)
        return [assistant]+copy.deepcopy(session['exchange'][1:])
    def _pause_continuation(self, parent):
        store=self.store(parent['project_id'])
        if (store.task().get('pause') or {}).get('reason')!='provider_continuation_unavailable':
            store.pause('provider_continuation_unavailable',{'parent_run_id':parent['id'],
                'event_id':store.session(parent['id'])['event_id']})
        self.manager._update(parent['id'],'director_paused','原Provider续接不可验证，保留claim等待核实。',status='awaiting_resume')
        if self.is_readonly(parent):
            self.manager.record_conversation_turn(parent['id'],kind='director',
                summary='原只读Provider结果续接包不可验证；保留原claim/费用，等待核实，不重发模型或实施修改。',status='awaiting_resume')
    def _delegate(self, parent, call):
        from .runtime import normalize_request
        from .director_creation import authority
        m=self.manager;creative=authority(self,parent)
        body=delegation(json.loads(call['function']['arguments']),allow_empty_objects=bool(creative))
        if self.is_readonly(parent):reject('只读请求不能委派施工。','director_readonly_violation',409)
        store=self.store(parent['project_id']); task=store.task()
        existing=next((record for record in store.bindings(parent['id']) if record['call_id']==call['id']),None)
        if existing:
            if {key:existing['brief'][key] for key in body}!=body:
                reject('原委派工具身份内容冲突。','director_delegation_conflict',409)
            return self._prepare_child(parent,existing)
        from . import director_request_scope as scopes
        if scopes.enabled(self,parent):
            scopes.gate(self,parent,'delegate_task',body)
        elif not task['policy']['autonomous'] or task['pause']: reject('持续自主施工未获授权或已暂停。','director_autonomy_paused',409)
        source='interactive' if scopes.interactive_execution(self,parent) else 'autonomous'
        store.economic_dispatch_admission(m._execution_profile(parent['profiles']['builder']),source=source)
        if not scopes.new_finite(self,parent):store.execution_admission(1 if scopes.managed(self,parent) else parent['builder_iteration']['max_rounds'],requestable=bool(parent.get('agent_resource_base')))
        if parent.get('director_ledger_unknown'): reject('费用attempt落盘待核实，不能派发写入。','director_ledger_unknown',409)
        with m.lock:
            if parent['cancel_requested']: reject('已取消后续委派。','cancelled',409)
            if creative:
                from .director_creation import select_stage
                select_stage(self,parent)
            if m._scene_workers_active(exclude=parent['id']): reject('场景写入者仍在执行。','busy',409)
            m._assert_asset_write(parent['project_id'],run_id=parent['id'])
            project=m.get_project(parent['project_id']); view,_=working(m,parent['project_id'])
            bootstrap=not view['selection']
            if bootstrap and (not creative or view['working_version']!=0):
                reject('空项目初建需要原可信创作授权。','working_scene_missing',409)
            if project['revision']!=parent['expected_project_revision']: reject('Director观察的项目基线已失效。','director_stale',409)
            from . import creative_continuation
            continuation=creative_continuation.current(self,parent)
            if continuation:
                creative_continuation.verify(self,parent,continuation)
                if continuation['mode']!='successor':reject('原Run可安全续接，请使用其现有资源批准入口。','creative_same_run_required',409)
                if continuation.get('kind')=='pending_edit' and set(body['item_ids'])!=set(continuation['pending_round']['item_ids']):
                    reject('待自检草稿须保留原工作项范围。','director_scope_denied',409)
                view={**view,'objects':copy.deepcopy(continuation['objects']),'assets':copy.deepcopy(continuation['assets'])}
            known={row['id'] for row in view['objects']}
            known.update(row['instance_id'] for row in view.get('assets',{}).get('instances',[]))
            if set(body['object_ids'])-known: reject('局部范围包含未知对象。')
            # Only the user's request creates the common Goal, at this construction boundary.
            m.bind_refinement_goal(parent['id'])
            parent=m._get(parent['id']); goal=m.common_goal(parent)
            m._assert_refinement_goal(parent)
            store.remember_goal(goal)
            brief={**body,'goal_ref':{k:copy.deepcopy(goal[k]) for k in ('id','version','content_digest')},
                'baseline':view,'parent_request':parent['prompt']}
            if scopes.managed(self,parent):
                brief['execution_saved_target']=scopes.execution_saved_target(self,parent)
                brief['execution_policy_version']=2
                from .agent_resources import execution_source
                resources=execution_source(self,parent)
                if resources:brief['execution_resources']=resources
            if creative:
                brief.update(creation_authorization=creative,bootstrap=bootstrap)
            if continuation:brief['creative_continuation']=copy.deepcopy(continuation)
            references=self.accepted_request(parent).get('reference_snapshots')
            if references is None and creative and not task.get('pause'):
                references=creative['approved'].get('reference_snapshots')
            if references is not None:brief['reference_snapshots']=copy.deepcopy(references)
            state=store.state.refinement_state(parent['id'])
            if creative:
                from .director_goal_plan import plan_authorization
                brief['plan_authorization']=plan_authorization(state['plan'],body['item_ids'])
            if state['binding'].get('director_metadata_id'):
                from .director_goal_plan import plan_authorization
                with store.state._connection() as c:
                    last=json.loads(c.execute('SELECT document FROM director_metadata WHERE parent_id=? ORDER BY rowid DESC LIMIT 1',
                        (parent['id'],)).fetchone()[0])
                if state['plan']['version']!=last['plan_version']:
                    reject('全局Plan已被外部更新，重新读取并明确决定。','director_metadata_stale',409)
                brief['plan_authorization']=plan_authorization(state['plan'],body['item_ids'])
            record=store.reserve(parent,call['id'],brief)
            return self._prepare_child(parent,record)

    def _prepare_child(self, parent, record):
        from .runtime import normalize_request
        m=self.manager; store=self.store(parent['project_id']); body=record['brief']; current=body['baseline']['selection']
        if record['state'] in {'started','waiting','completed','rejected'}:
            reject('原子委派已开始，不能再次准备/启动。','director_child_not_replayable',409)
        if record['state']=='reserved':
            if body.get('execution_resources'):
                from .agent_resources import execution_source
                if execution_source(self,parent,exclude=record['child_id'])!=body['execution_resources']:
                    reject('前序执行额度已变化，不能重建后继预算。','resource_base_conflict',409)
            if body.get('creative_continuation'):
                from .creative_continuation import verify
                verify(self,parent,body['creative_continuation'],child_id=record['child_id'])
            accepted=self.accepted_request(parent)
            bootstrap=body.get('bootstrap') is True
            from .director_creation import ensure_local_authority
            local=ensure_local_authority(self,parent)
            value=record.get('request') or {'operation':'generate' if bootstrap else 'edit','project_id':parent['project_id'],
                'expected_project_revision':body['baseline']['project_revision'],
                **({'base_scene':{k:current[k] for k in ('run_id','revision_id')}} if current else {}),'prompt':body['goal'],
                'profiles':{'brain':parent['profiles']['builder']},'constraints':parent['constraints'],
                'execution':parent['execution'],'iteration':{'enabled':True,'max_rounds':1} if bootstrap else {**parent['builder_iteration'],'max_rounds':body.get('execution_saved_target',parent['builder_iteration']['max_rounds'])},'judge':accepted['judge'],
                'evaluation':{'mode':'reference' if any(r['purpose']=='scene' for r in body.get('reference_snapshots',[])) else 'intent'},'cost_policy':'call_limits_only',
                'idempotency_key':'delegate-'+record['delegation_id']}
            if not bootstrap and not record.get('request') and accepted.get('asset_generation'):
                value['asset_generation']=copy.deepcopy(accepted['asset_generation'])
            if local:
                value['asset_generation']={'enabled':True,'profile_id':body['creation_authorization']['approved']['local_profile_snapshot']['id']}
            elif (body.get('execution_resources') or {}).get('excluded_asset_jobs'):
                # The original closed unknown cloud job/approval stays owned
                # by its old Run. A new child gets no replacement cloud slot.
                value.pop('asset_generation',None)
            if bootstrap and not record.get('request') and 'max_calls' not in accepted['judge']:
                from .agent_resources import BOOTSTRAP
                value['judge'] = {**copy.deepcopy(accepted['judge']), 'max_calls': BOOTSTRAP['judge_calls']}
            creation_unstarted = (not record.get('request') and not record.get('child_snapshot')
                and record['child_id'] not in m.runs
                and not (m._projects().runs_root(parent['project_id']) / record['child_id']).exists())
            try:
                record['submission_digest']=digest(normalize_request(value,_bootstrap=bootstrap))
            except Exception as error:
                # This exact call precedes create(). Validation rejection is
                # proven zero execution; other failures retain the reserved claim.
                from .runtime import AppError
                if creation_unstarted and isinstance(error, AppError) and error.status < 500:
                    record.update(state='rejected', request=copy.deepcopy(value), rejection={
                        'code': error.code, 'execution': 'confirmed_not_created'})
                    store.update(record)
                    if error.code == 'invalid_judge_settings':
                        raise AppError('任务的裁判预算配置不一致，施工未能开始，请等待修复后再试。',
                                       error.code, error.status) from None
                raise
            record['request']=copy.deepcopy(value); store.update(record)
            child=m.create(value,_director_delegation=record)
            record['child_snapshot']=copy.deepcopy(child)
            record.update(state='prepared'); store.update(record)
        return {'accepted':True,'delegation_id':record['delegation_id'],'child_run_id':record['child_id'],
                'task_id':record['task_id'],'state':'prepared'}
    def execute(self, identifier, result=None):
        from .providers import brain_timeout
        m=self.manager; parent=m._get(identifier); store=self.store(parent['project_id'])
        readonly=self.is_readonly(parent)
        try:
            while True:
                if m._cancelled(identifier): return
                readonly=self.is_readonly(parent)
                task=store.task()
                if not readonly and task['policy']['mode']=='strict' and store.cost_source is None:
                    store.pause('price_upper_bound_missing',{'parent_run_id':identifier,'missing':'trusted model charge upper bound'})
                    m._update(identifier,'director_paused','缺少可信费用上界，保留任务等待人类核实。',status='awaiting_resume')
                    return
                session=store.session(identifier)
                event=store.event(session['event_id'])
                result=event.get('result')
                # Facts are public. Native continuation is decrypted only when
                # preparing a new Provider request, never used as public audit.
                if readonly:
                    with m.lock,m._projects().lock:
                        messages,facts=director_context.request(m,parent,task,result=result)
                else: messages,facts=director_context.request(m,parent,task,result=result)
                from . import director_reply_correction as correction
                repair=correction.window(store,parent,facts)
                if repair and not repair['remaining']:
                    m._update(identifier,'director_paused','两次回复纠正未完成；施工暂停及原结果保留。',status='awaiting_resume')
                    return
                decision_record=store.prepare_decision(identifier,event['event_id'],facts,
                    call_index=parent['usage']['director_calls']+1,
                    report_pause_id=(task.get('pause') or {}).get('pause_id') if correction.known_result(store,parent,facts) else None)
                if decision_record['state'] in {'started','unknown'}:
                    store.pause('decision_outcome_unknown',{'decision_id':decision_record['decision_id']})
                    reject('原Director请求已开始或结果未知，不能重发。','director_decision_not_replayable',409)
                if decision_record['state']=='applied':
                    self._repair_reply(identifier)
                    return
                if decision_record['state']=='returned':
                    if readonly and 'readonly_snapshot' in decision_record['input']:
                        facts.update(copy.deepcopy(decision_record['input']['readonly_snapshot']))
                    elif readonly and not session.get('natural_recovery'):
                        reject('原只读输入快照缺失，不能猜补。','director_readonly_snapshot_missing',409)
                    assistant=self._private(parent).load(decision_record.get('private_ref'),decision_record)
                else:
                    from .director_request_scope import enabled as scoped_request
                    try:store.admission('interactive' if scoped_request(self,parent) else event['source'])
                    except Exception as error:
                        if not readonly:raise
                        from .director_readonly import blocked_reply
                        store.readonly_status(identifier,blocked_reply(facts,getattr(error,'code','director_economy_paused')))
                        self._repair_reply(identifier);return
                    exchange=self._native_exchange(parent,session)
                    messages[2:2]=exchange
                    if repair and exchange and not exchange[0].get('tool_calls'):
                        messages.insert(-1,{'role':'user','content':json.dumps({'error':'director_reply_required',
                            'instruction':'Only report the known result with director_reply or director_clarify; construction remains paused.',
                            'expected':{'result_id':facts['result']['result_id'],'working_version':facts['working']['working_version']}},ensure_ascii=False)})
                    with m.lock:
                        parent['usage']['director_calls']=decision_record['call_index']; m._save(parent)
                    from . import director_request_scope as scopes
                    from .director_creation import tools as creation_tools
                    available=scopes.tools(self,parent) if scopes.enabled(self,parent) else READONLY_TOOLS if readonly else [tool for tool in creation_tools(self,parent) if not task.get('pause') or tool['function']['name']=='director_reply']
                    resource_paused=scopes.pause_applies(self,parent) if scopes.enabled(self,parent) else bool(task.get('pause'))
                    if parent.get('agent_resource_base') and not readonly and not resource_paused and not scopes.stopping_reason(self,parent):
                        from .agent_resources import TOOLS as RESOURCE_TOOLS
                        if not scopes.enabled(self,parent) or (scopes.current(self,parent) or {}).get('intent') in {'edit','continuous'}:
                            available=available+[RESOURCE_TOOLS[1]]
                    if repair:
                        available=[q for q in available if q['function']['name'] in correction.REPLY_TOOLS]
                    assistant=m._agent_step(identifier,m._execution_profile(parent['profiles']['brain']),messages,available,
                        role='director',timeout=brain_timeout(parent['profiles']['brain']))
                    reference=self._private(parent).save(decision_record,assistant,'response')
                    store.response_confirmed(event['event_id'],assistant,private_ref=reference)
                from .providers import _agent_calls
                # Validate the outer package before inspecting names. Director
                # argument JSON remains eligible for its existing paired feedback.
                calls=_agent_calls(assistant.get('tool_calls'),director_arguments=True)
                from . import director_request_scope as scopes
                bad_arguments=False
                for q in calls:
                    try:bad_arguments=bad_arguments or not isinstance(json.loads(q['function']['arguments']),dict)
                    except (ValueError,TypeError):bad_arguments=True
                legacy_readonly_rejection=(readonly and not scopes.enabled(self,parent) and
                    (len(calls)!=1 or calls[0]['function']['name'] not in {'director_reply','director_clarify'}))
                resource_call=any(q['function']['name']=='request_resources' for q in calls)
                if not legacy_readonly_rejection and (scopes.enabled(self,parent) or resource_call or len(calls)>1 or decision_record.get('batch') or bad_arguments or not calls and assistant.get('content')):
                    from .director_batch import process
                    if process(self,parent,event,decision_record,assistant,facts):continue
                    return
                if readonly and (len(calls)!=1 or calls[0]['function']['name'] not in {'director_reply','director_clarify'}):
                    decision={'summary':'只读能力拒绝模型的写入/渲染或无效调用；本次未实施修改。',
                        'next_focus':'仅可回复或澄清。','as_of':facts['as_of'],'read_only':True,
                        'source':'runtime_rejection','status':'failed'}
                    exchange=[assistant]+[{'role':'tool','tool_call_id':row['id'],
                        'content':json.dumps({'ok':False,'error':'director_readonly_violation'})} for row in calls]
                    reference=self._private(parent).save(decision_record,assistant,'exchange',tools=exchange[1:])
                    store.apply_decision(identifier,event['event_id'],exchange,decision=decision,private_ref=reference)
                    self._repair_reply(identifier);return
                if not calls: reject('Director需通过工具保存明确决策。','director_decision_missing',409)
                if len(calls)!=1:
                    # Pair every native call, then return a bounded precise correction.
                    exchange=[assistant]+[{'role':'tool','tool_call_id':c['id'],
                        'content':json.dumps({'ok':False,'error':'One effect delegation or one decision per boundary.'})} for c in calls]
                    store.pause('decision_boundary_invalid',{'call_count':len(calls)})
                    reject('一次决策边界不能并行委派或同时回复。','director_boundary_invalid',409)
                call=calls[0]; name=call['function']['name']
                if readonly and name=='director_clarify':name='director_reply'
                if name=='director_observe':
                    from .observation_service import ObservationCache
                    from .observation_contract import normalize_view_spec
                    body=json.loads(call['function']['arguments'])
                    if not isinstance(body,dict) or set(body)!={'view'}: reject('观察只接受受控view。')
                    try: view=normalize_view_spec(body['view'])
                    except ValueError as error: reject(str(error),'observation_invalid',400)
                    store.admission(event['source'])
                    project=m.get_project(parent['project_id'])
                    if not project.get('current_scene'): reject('缺少Working观察来源。','working_scene_missing',409)
                    if project['revision']!=parent['expected_project_revision']: reject('观察项目基线已变化。','director_stale',409)
                    version=m.scene_version(project['current_scene']['run_id'],project['current_scene']['revision_id'])
                    cache=ObservationCache(m,parent['project_id'],m.blender.create_adapter(mode='headless'))
                    observed=cache.observe(parent,version,view,digest({'parent_id':identifier,'event_id':event['event_id'],'call_id':call['id']}))
                    record=observed['record']
                    exchange=[assistant,{'role':'tool','tool_call_id':call['id'],'content':json.dumps({'ok':True,
                        'result':{'ref':record['ref'],'identity':record['identity'],'observation':record['observation'],
                                  'cache_hit':observed['cache_hit'],'renders':observed['renders'],'bytes':observed['bytes']}})}]
                    reference=self._private(parent).save(decision_record,assistant,'exchange',tools=exchange[1:])
                    store.apply_decision(identifier,event['event_id'],exchange,private_ref=reference,observation=record)
                    if store.task().get('consecutive_observation_repeats',0)>=2:
                        store.pause('repeated_observation_no_new_evidence',{'identity':record['identity']})
                    if store.task().get('pause'):
                        m._update(identifier,'director_paused','观察结果已保留；资源或重复无新证据，等待人类核实。',status='awaiting_resume')
                        return
                    m._save(parent)
                    continue
                if name=='delegate_task':
                    accepted=self._delegate(parent,call)
                    exchange=[assistant,{'role':'tool','tool_call_id':call['id'],
                        'content':json.dumps({'ok':True,'result':accepted})}]
                    reference=self._private(parent).save(decision_record,assistant,'exchange',tools=exchange[1:])
                    store.apply_decision(identifier,event['event_id'],exchange,child_id=accepted['child_run_id'],private_ref=reference)
                    parent.update(status='waiting_child',stage='director_waiting',active_child_id=accepted['child_run_id'])
                    m._save(parent)
                    return
                if name=='director_goal_plan':
                    from .director_goal_plan import apply
                    from .scene_evidence import known_object_ids
                    with m.lock,m._projects().lock:
                        body=json.loads(call['function']['arguments'])
                        metadata=store.metadata_receipt(identifier,decision_record,call,body)
                        if metadata is None:
                            m._assert_asset_write(parent['project_id'],run_id=identifier)
                            if m._scene_workers_active(exclude=identifier):
                                reject('活跃施工期间不能写全局Goal/Plan。','director_metadata_busy',409)
                            project=m.get_project(parent['project_id'])
                            current=project.get('current_scene')
                            from .director_creation import authority
                            if not current and not authority(self,parent):reject('元数据施工边界需要已有Working。','working_scene_missing',409)
                            version=m.scene_version(current['run_id'],current['revision_id']) if current else None
                            measured=json.loads(version['files']['inspection'].read_text(encoding='utf-8')) if version else {}
                            metadata=apply(store,parent,decision_record,call,body,
                                known_ids=known_object_ids(version['plan'],measured) if version else [])
                            m.bind_refinement_goal(identifier)
                            parent=m._get(identifier)
                        else:
                            # Read the original SQL binding on ACK recovery;
                            # active/stale state must not create another write.
                            binding=store.state.refinement_state(identifier)['binding']
                            if not binding or {k:binding['goal'][k] for k in ('id','version','content_digest')}!=metadata['goal_ref']:
                                reject('原元数据绑定缺失，不能猜补。','director_metadata_unbound',409)
                            parent.update(goal_snapshot=copy.deepcopy(binding['goal']),
                                refinement_binding={k:copy.deepcopy(v) for k,v in binding.items() if k!='goal'},
                                constraints=copy.deepcopy(binding['goal']['content']['constraints']))
                    exchange=[assistant,{'role':'tool','tool_call_id':call['id'],'content':json.dumps({'ok':True,'result':metadata})}]
                    reference=self._private(parent).save(decision_record,assistant,'exchange',tools=exchange[1:])
                    store.apply_decision(identifier,event['event_id'],exchange,private_ref=reference,metadata=metadata)
                    m._save(parent)
                    if store.task().get('pause'):
                        m._update(identifier,'director_paused','元数据及完整工具回执已保存；无新增实质状态或原暂停保持，等待人类核实。',status='awaiting_resume')
                        return
                    continue
                if name!='director_reply': reject('未知Director工具。')
                body=json.loads(call['function']['arguments'])
                if (set(body)-{'summary','next_focus','result_id','working_version'}
                    or any(not isinstance(body.get(key),str) or not 1<=len(body[key])<=4000 for key in ('summary','next_focus'))): reject('Director回复字段无效。')
                if result and (body.get('result_id')!=result['result_id'] or body.get('working_version')!=facts['working']['working_version']):
                    reject('下一决策必须引用刚消费的真实结果及Working版本。','director_result_reference_invalid',409)
                decision={**body,'task_id':task['task_id'],'working_version':facts['working']['working_version'],
                    'input_image_sha256':[row['sha256'] for row in facts['working']['images']]}
                if readonly:decision.update(read_only=True,as_of=copy.deepcopy(facts['as_of']),source='model_readonly')
                exchange=[assistant,{'role':'tool','tool_call_id':call['id'],'content':json.dumps({'ok':True,'decision':decision})}]
                reference=self._private(parent).save(decision_record,assistant,'exchange',tools=exchange[1:])
                store.apply_decision(identifier,event['event_id'],exchange,decision=decision,private_ref=reference)
                parent.update(status='completed',stage='director_completed',director_decision=decision)
                m._save(parent)
                self._repair_reply(identifier)
                return
        except Exception as error:
            if getattr(error,'code',None)=='director_continuation_unavailable':
                self._pause_continuation(parent)
                return
            session=store.session(identifier)
            uncertain=store.decision_for(session['event_id']) if session else None
            if readonly and getattr(error,'code',None)=='director_economy_paused' and (not uncertain or uncertain['state']=='prepared'):
                from .director_readonly import blocked_reply
                parent['usage']['director_calls']=max(0,parent['usage']['director_calls']-1)
                store.readonly_status(identifier,blocked_reply(facts,error.code))
                self._repair_reply(identifier);return
            if uncertain and uncertain['state']=='started' and not store.task().get('pause'):
                store.pause('decision_outcome_unknown',{'decision_id':uncertain['decision_id']})
            if getattr(error,'code',None) in {'director_autonomy_paused','director_economy_paused','observation_storage_limit','observation_outcome_unknown'}:
                m._update(identifier,'director_paused',str(error),status='awaiting_resume')
            else:
                m._fail(identifier,error)
            if readonly:
                try:m.record_conversation_turn(identifier,kind='director',summary='只读请求未完成；原模型结果/费用需按原claim核实，不自动重发，未实施修改。',status=m.runs[identifier]['status'])
                except Exception:parent['turn_record_pending']=True;m._save(parent)
    def _repair_reply(self, identifier):
        m=self.manager; parent=m.runs[identifier]; store=self.store(parent['project_id']); session=store.session(identifier)
        decision=session.get('decision')
        if not decision: return
        readonly=self.is_readonly(parent)
        status=('cancelled' if parent['cancel_requested'] else decision.get('status','completed')) if readonly else 'completed'
        parent.update(status=status,stage='director_'+status,director_decision=decision)
        m._save(parent)
        self._repairing_turns.add(identifier)
        try:
            turns=m.get_project(parent['project_id'])['workspace']['turns']
            if session.get('turn_pending') or not any(row['id']==identifier for row in turns):
                m.record_conversation_turn(identifier,kind='director',summary=director_context.reply_text(decision['summary'],session.get('result'),decision.get('next_focus','')),status=status if readonly else 'completed',
                    evidence=[{'child_run_id':r['child_id'],'result_id':(r['result'] or {}).get('result_id')}
                              for r in store.bindings(identifier)])
            session.update(turn_pending=False,state='completed'); store.save_session(session)
            parent['turn_record_pending']=False; m._save(parent)
        except Exception:
            parent['turn_record_pending']=True; m._save(parent)
        finally:self._repairing_turns.discard(identifier)
    def _zero_write_syntax_failure(self, record, result):
        """Narrow bootstrap evidence, never permission to replay a Builder call."""
        try:
            m=self.manager;run=m.runs[record['child_id']];parent=m.runs[record['parent_id']]
            from .director_creation import authority
            approved=authority(self,parent);brief=record['brief'];base=brief['baseline']
            error=result.get('error') or {};diag=error.get('diagnostics') or {};parse=diag.get('response_parse') or {}
            correction=run.get('builder_tool_correction') or {}
            calls=1
            if error.get('code') in {'tool_arguments_correction_exhausted','tool_correction_budget_exhausted'}:
                batches=correction.get('batches') or [];requests=correction.get('requests') or []
                spent=correction.get('correction_calls')
                if (correction.get('run_id')!=run['id'] or type(spent) is not int or not 0<=spent<=2
                        or len(requests)!=spent or len(batches)!=spent+1 or not correction.get('pending')
                        or any(r.get('status')!='returned_invalid' for r in requests)
                        or any(b.get('execution')!='not_executed' or len(b.get('calls',[]))!=1
                               or b['calls'][0]['name']!='create_scene' or not b.get('errors')
                               or any(e.get('reason')!='syntax' for e in b['errors']) for b in batches)):return None
                calls=spent+1
            if (not approved or brief.get('creation_authorization')!=approved or not brief.get('bootstrap')
                    or not run.get('bootstrap') or parent.get('cancel_requested') or run.get('cancel_requested')
                    or run.get('director_ledger_unknown') or result['status']!='failed'
                    or error.get('code') not in {'invalid_json','tool_arguments_correction_exhausted','tool_correction_budget_exhausted'}
                    or error.get('outcome_unknown') is not False
                    or parse.get('source')!='provider_response' or parse.get('layer')!='tool_arguments'
                    or parse.get('reason')!='syntax' or parse.get('tool_name')!='create_scene' or parse.get('tool_index')!=0
                    or parse.get('input_relative_to')!='tool_arguments' or parse.get('position_known') is not True
                    or diag.get('finish_reason')!='tool_calls' or diag.get('returned_tool_calls')!=1
                    or parse.get('brain_call')!=calls or parse.get('run_id')!=run['id']):return None
            position=parse.get('position') or {}
            if position.get('unit')!='char' or type(position.get('offset')) is not int or position['offset']<0:return None
            project=m.get_project(run['project_id']);usage=run['usage'];agent=run.get('agent') or {}
            if (base['selection'] is not None or base['working_version']!=0
                    or project['current_scene'] is not None or project['workspace']['working_version']!=0
                    or project['revision']!=base['project_revision'] or usage.get('brain_calls')!=calls
                    or any(usage.get(k)!=0 for k in ('image_calls','judge_calls','asset3d_calls'))
                    or any(agent.get('used',{}).get(k)!=0 for k in ('tool_calls','edit_rounds','observations'))
                    or any(agent.get(k) for k in ('revisions','actions','observations','selected_revision','pending_revision','uncertain_write'))
                    or run.get('commits') or run.get('initial_working_commit') or result['evaluations']
                    or any(a.get('kind')!='concept' for a in run.get('artifacts',[]))):return None
            from .project_runtime import is_link
            candidates=m.run_directory(run['id'])/'candidates'
            if is_link(candidates) or candidates.exists() and any(candidates.iterdir()):return None
            state=self.store(run['project_id']).state
            with state._connection() as c:
                for table in ('operations','receipts','local_rounds','asset_run_requests','evaluation_inputs','evaluation_receipts'):
                    if c.execute('SELECT 1 FROM '+table+' WHERE run_id=? LIMIT 1',(run['id'],)).fetchone():return None
                if c.execute('SELECT 1 FROM asset_generation_jobs LIMIT 1').fetchone():return None
            return {'kind':'bootstrap_tool_json_not_executed','execution':'confirmed_not_executed',
                'layer':'tool_arguments','reason':'syntax','tool_name':'create_scene','position':copy.deepcopy(position),
                'guidance':'首版工具参数未形成有效JSON，确认没有执行施工或保存。请自主缩小首版、拆分细节并重新规划新委派，或向用户说明；不要重发原Builder请求。'}
        except Exception:
            return None

    def _known_length_failure(self, record, result):
        """Return a known bounded Builder failure for Director replanning only.

        This never grants a new call or resumes the failed child. Unknown
        calls/writes, changed baselines and cancellation retain the old pause.
        """
        try:
            m = self.manager
            run = m.get(record['child_id'])
            error = result.get('error') or {}
            recovery = (run.get('agent') or {}).get('builder_recovery') or {}
            if (result.get('status') != 'failed' or error.get('code') not in {
                    'builder_length_recovery_exhausted', 'builder_length_budget_exhausted'}
                    or error.get('outcome_unknown') is not False or run.get('cancel_requested')
                    or m.get(record['parent_id']).get('cancel_requested')
                    or recovery.get('run_id') != run['id'] or not recovery.get('pending')
                    or recovery.get('last_error', {}).get('execution') != 'confirmed_zero_tools'
                    or type(recovery.get('recovery_calls')) is not int
                    or not 0 <= recovery['recovery_calls'] <= 2
                    or len(recovery.get('attempts', [])) != recovery['recovery_calls']
                    or any(a.get('status') not in {'returned', 'failed'} for a in recovery['attempts'])):
                return None
            from .tool_correction import BuilderCorrection
            BuilderCorrection(m, run['id']).guard()
            m._verify_project_baseline(run)
            m._assert_refinement_goal(run)
            import json
            with self.store(run['project_id']).state._connection() as connection:
                for row in connection.execute('SELECT document FROM director_attempts WHERE task_id=?', (record['task_id'],)):
                    attempt = json.loads(row[0])
                    if attempt.get('state') != 'settled' or attempt.get('settlement', {}).get('outcome') != 'returned':
                        return None
            return {'kind': 'builder_output_recovery_exhausted', 'execution': 'truncated_responses_not_executed',
                'attempts': copy.deepcopy(recovery['attempts']), 'recovery_calls': recovery['recovery_calls'],
                'last_error': copy.deepcopy(recovery['last_error']),
                'guidance': '原Builder单次输出触顶，本地有界收束已结束；已发生施工以真实回执为准。请依据实际结果决定缩小任务、申请资源或结束，不重放原请求，不代替Builder修参数。'}
        except Exception:
            return None

    def result(self, record):
        m=self.manager; run=m.get(record['child_id']); project=m.get_project(run['project_id'])
        current,_=working(m,run['project_id']); before=record['brief']['baseline']
        original={row['id']:row for row in before['objects']}; latest={row['id']:row for row in current['objects']}
        changes=[]
        for key in sorted(set(original)|set(latest)):
            old,new=original.get(key) or {},latest.get(key) or {}
            fields=[field for field in sorted(set(old)|set(new)) if old.get(field)!=new.get(field)]
            if fields:
                changes.append({'object_id':key,'changed_fields':fields,
                    'before':{field:old.get(field) for field in fields},'after':{field:new.get(field) for field in fields}})
        old_assets={row['instance_id']:row for row in before.get('assets',{}).get('instances',[])}
        new_assets={row['instance_id']:row for row in current.get('assets',{}).get('instances',[])}
        for key in sorted(set(old_assets)|set(new_assets)):
            old=old_assets.get(key) or {};new=new_assets.get(key) or {}
            if old!=new:
                changes.append({'object_id':key,'kind':'asset_instance','changed_fields':['asset_identity_or_transform'],
                    'before':copy.deepcopy(old.get('transform',{})), 'after':copy.deepcopy(new.get('transform',{}))})
        old_surface=(before.get('inspection') or {}).get('surface') or {}
        new_surface=(current.get('inspection') or {}).get('surface') or {}
        if old_surface.get('state_digest')!=new_surface.get('state_digest'):
            changes.append({'object_id':'scene-appearance','kind':'appearance','changed_fields':['surface_state'],
                'before':{'surface_state_digest':old_surface.get('state_digest')},
                'after':{'surface_state_digest':new_surface.get('state_digest'),'verified':new_surface.get('verified') is True}})
        history=[]
        for row in run.get('judge_history',[])[-2:]:
            judge=row.get('judgement') or {}
            receipt=row.get('application_receipt') or {}
            history.append({'status':row['status'],'input_id':row.get('input_id'),'evaluation_id':receipt.get('evaluation_id'),
                'application_status':receipt.get('application_status'),'verdict':judge.get('verdict'),
                'target':{k:copy.deepcopy((judge.get('target') or {}).get(k)) for k in
                          ('kind','before','current','working_version','goal_digest','scope','stage')},
                'item_results':copy.deepcopy(judge.get('item_results',[])[:16]),
                'issue_reviews':copy.deepcopy(judge.get('issue_reviews',[])[:16]),
                'next_focus':copy.deepcopy(judge.get('next_focus',[])[:16]),
                'unknowns':copy.deepcopy(judge.get('unknowns',[])[:16]),'format_error':judge.get('format_error'),
                'message':judge.get('message'),'unknown':bool(row.get('unknown'))})
        result={'result_id':digest({'delegation':record['delegation_id'],'child':run['id']}),
            'task_id':record['task_id'],'delegation_id':record['delegation_id'],'child_run_id':run['id'],
            'status':run['status'],'stop_reason':(run.get('agent') or {}).get('stop_reason'),
            'builder_self_check':[copy.deepcopy(row['assessment']) for row in (run.get('agent') or {}).get('revisions',[])
                                  if row.get('assessment')][-2:],
            'commits':copy.deepcopy(run.get('commits',[])[-12:]),'evaluations':history,
            'initial_working_commit':copy.deepcopy(run.get('initial_working_commit')),
            'saved_edit_rounds':m._projects().state_store(run['project_id']).readiness(run['id'])['saved_edit_rounds'],
            'draft_attempts':m._projects().state_store(run['project_id']).local_edit_attempt_count(run['id']),
            'before':director_context.selection_view(before),'working':director_context.selection_view(current),
            'feedback_at_completion':director_context.completion_feedback(director_context.feedback(m,run['project_id'])),
            'net_changes':changes,'usage':run['usage'],'error':run.get('error'),
            'execution_url':'/api/runs/'+run['id']}
        from .director_readonly import asset_facts
        result['assets']=asset_facts(m,run['project_id'],record['task_id'])
        result['construction']=director_context.construction_facts(m,run)
        if run.get('creative_resource_feedback'):
            result['resource_request']=copy.deepcopy(run['creative_resource_feedback'])
        correction=run.get('builder_tool_correction')
        if correction:
            result['tool_argument_correction']={k:copy.deepcopy(correction[k]) for k in ('correction_calls','requests','batches')}
        recovery=self._zero_write_syntax_failure(record,result) or self._known_length_failure(record,result)
        if recovery:result['director_recovery']=recovery
        result['digest']=digest(result)
        return result
    def finished(self, identifier):
        m=self.manager; run=m.runs[identifier]
        if not run.get('project_id'): return False
        if run.get('operation')=='conversation':
            from .director_request_scope import enabled,schedule
            if enabled(self,run) and run['status'] in {'completed','failed','cancelled','awaiting_resume','queued'}:
                schedule(self,run['project_id'],exclude=identifier)
                return True
            if self.is_readonly(run):return True
            if run['status']=='waiting_child':
                record=self.store(run['project_id']).child(run['active_child_id'])
                if record and record['state']=='prepared':
                    self._dispatch(record)
                return True
            return False
        if not run.get('director_delegation'):
            from .director_request_scope import schedule
            if run['status'] in {'completed','failed','cancelled'}:schedule(self,run['project_id'],exclude=identifier)
            return False
        record=self.binding(identifier,run['project_id'])
        if not record: return False
        store=self.store(run['project_id'])
        if run['status'] not in {'completed','failed','interrupted','cancelled'}:
            record['state']='waiting'; store.update(record)
            return True
        from .resource_parent_recovery import reconcile_local_failure_parent
        reconcile_local_failure_parent(m,m.runs[record['parent_id']],finishing_child=identifier)
        result=record.get('result') or self.result(record); event=store.finish(record,result)
        store.progress(record,result)
        if store.event(event)['consumed']:return True
        recoverable=bool(result.get('director_recovery') and (
            self._zero_write_syntax_failure(record,result) or self._known_length_failure(record,result)))
        from .builder_terminal import blocker
        from .director_request_scope import managed
        parent=m.runs[record['parent_id']]
        terminal_blocker=blocker(self,record,finishing=True) if managed(self,parent) else None
        needs_review=((result['error'] and not recoverable) or result['stop_reason'] in {'stalled','no_progress','needs_user_review'}
                or any(row['unknown'] or row['verdict']=='uncertain' or row['status']=='failed' for row in result['evaluations']))
        if (needs_review and not managed(self,parent)) or terminal_blocker or run.get('cancel_requested') or run['status'] in {'cancelled','interrupted'}:
            store.pause('child_requires_review',{'result_id':result['result_id'],'child_run_id':identifier,
                **({'blocker':terminal_blocker} if terminal_blocker else {})})
        parent=m.runs[record['parent_id']]
        store.attach_result(record['parent_id'],event,result)
        if not store.event(event)['consumed'] and not parent['cancel_requested']:
            parent.update(status='running',stage='director_result',expected_project_revision=m.get_project(run['project_id'])['revision'])
            m._save(parent)
            try:
                from .director_request_scope import enabled
                store.admission('interactive' if enabled(self,parent) else 'autonomous')
            except Exception as error:
                m._update(parent['id'],'director_paused',str(error),status='awaiting_resume')
                return False
            else:
                m._start(parent,self.execute)
        return not parent['cancel_requested']
    def cancel(self, parent):
        from .director_request_scope import cancel_queued
        if cancel_queued(self,parent):return
        if self.is_readonly(parent):
            self.manager.record_conversation_turn(parent['id'],kind='director',status='cancelled',
                summary='已取消本只读请求的后续步骤；已提交模型可能继续执行或计费，原主父/施工/资产不受此次取消影响。')
            return
        store=self.store(parent['project_id'])
        pause=store.task().get('pause') or {}
        if pause.get('reason')!='user_cancelled' or (pause.get('evidence') or {}).get('parent_run_id')!=parent['id']:
            store.pause('user_cancelled',{'parent_run_id':parent['id'],'scope':'parent_and_active_child'})
        from .director_creation import stop_local_authority
        stop_local_authority(self,parent)
        for record in store.bindings(parent['id']):
            if record['state'] in {'prepared','started','waiting'}:
                child=self.manager.runs.get(record['child_id'],{})
                if child.get('local_asset_authorization') and hasattr(self.manager.asset_host,'cancel_local'):
                    self.manager.asset_host.cancel_local(self.manager,child['id'])
                self.manager.cancel(record['child_id'])
    def model_attempt(self, run, role, index, profile):
        store=self.store(run['project_id'])
        if run.get('director_ledger_unknown'): reject('原费用attempt结果待核实。','director_ledger_unknown',409)
        if run.get('operation')=='conversation':
            if not store.parent(run['id']): reject('缺少父任务关联。','director_unbound',409)
            session=store.session(run['id']); event=store.event(session['event_id'])
            from .director_request_scope import enabled
            source='interactive' if enabled(self,run) else event['source'] if not event['consumed'] else 'interactive' if index==1 else 'autonomous'
            store.admission(source)
            decision=store.decision_for(event['event_id'])
            if not decision or decision['state']!='prepared': reject('没有已确认未开始的决策claim。','director_claim_missing',409)
        elif store.child(run['id']):
            source=self.child_source(run)
        else: return None
        record=store.attempt(run['id'],role,index,profile,source,event_id=event['event_id'] if role=='director' else None)
        return store,record

    def child_source(self, run):
        """A finite accepted edit also owns its Builder/Judge and round gates."""
        store=self.store(run['project_id']);binding=store.child(run['id'])
        if not binding:return 'autonomous'
        parent=self.manager.runs[binding['parent_id']]
        from .director_request_scope import enabled,current,pause_applies
        from .director_request_scope import interactive_execution
        if interactive_execution(self,parent):
            return 'interactive'
        return 'autonomous'

    def child_admission(self, run):
        self.store(run['project_id']).admission(self.child_source(run))

    def local_asset_authorization(self, run):
        from .director_creation import local_authorization
        return local_authorization(self,run)

    def _dispatch(self, record):
        m=self.manager; store=self.store(m.runs[record['parent_id']]['project_id'])
        parent=m.runs[record['parent_id']]
        try:
            self._native_exchange(parent,store.session(parent['id']))
        except Exception as error:
            if getattr(error,'code',None)=='director_continuation_unavailable': self._pause_continuation(parent)
            raise
        from .director_request_scope import enabled,current,pause_applies
        from .director_request_scope import interactive_execution
        store.admission('interactive' if interactive_execution(self,parent) else 'autonomous')
        current,_=working(m,m.runs[record['parent_id']]['project_id'])
        baseline=record['brief']['baseline']
        for key in ('selection','working_version','blend_sha256','plan_sha256'):
            if current.get(key)!=baseline.get(key): reject('原委派基线已失效，不能静默改绑。','director_stale',409)
        if store.claim_child(record['child_id']):
            child=m.runs[record['child_id']]
            child.update(status='running',error=None)
            try:
                m._save(child); m._start(child,m._bootstrap_scene if child.get('bootstrap') else m._edit_scene)
            except Exception:
                store.pause('child_dispatch_unknown',{'child_id':record['child_id']})
                raise

    def hydrate(self):
        """Publish known local parent metadata/confirmed Turns; never start a model or child."""
        m=self.manager
        if m.project_repository is None: return
        for project in m._projects().list():
            if not project.get('available',True): continue
            state=m._projects().state_store(project['id'])
            with state._connection() as connection:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='director_sessions'").fetchone(): continue
            store=self.store(project['id'])
            for session in store.sessions():
                rid=session['parent_id']
                if rid not in m.runs:
                    snapshot=copy.deepcopy(session['run'])
                    snapshot.update(director_task_id=store.parent(rid)['task_id'],status='interrupted',
                        error={'code':'director_recovery_required','message':'本地准备/决策claim保留；不自动调用模型。','outcome_unknown':False})
                    m.runs[rid]=snapshot; m.run_directories[rid]=m._projects().runs_root(project['id'])/rid
                    m._save(snapshot)
                queue=session.get('queue') or {}
                claim=store.decision_for(session['event_id'])
                cancelled=m.runs[rid].get('cancel_requested')
                if cancelled and queue.get('state') in {'accepted','dispatching'} and (not claim or claim['state']=='prepared'):
                    from .director_request_scope import cancel_queued
                    cancel_queued(self,m.runs[rid]);m._cancelled(rid)
                if not cancelled and queue.get('state') in {'accepted','dispatching'} and (not claim or claim['state']=='prepared' or
                        queue['state']=='accepted' and claim['state']=='applied'):
                    # Known unstarted queue work retains its original Turn and scope.
                    # Hydration never invokes a model; idle scheduling/recover owns it.
                    queue.update(state='accepted',rebinding_required=True)
                    if not session.get('request_scope') and queue.get('reinterpret_at_idle'):
                        session['request_scope']=copy.deepcopy(queue['original_interpretation'])
                    session['request_scope']['state']='queued';store.save_session(session)
                    m.runs[rid].update(status='queued',stage='director_queued',error=None);m._save(m.runs[rid])
                elif session.get('decision'):
                    self._repair_reply(rid)
                elif store.readonly(rid) and not (m.workers.get(rid) and m.workers[rid].is_alive()):
                    decision=store.decision_for(session['event_id'])
                    if decision and decision['state'] in {'started','unknown'} and (store.task().get('pause') or {}).get('scope')!='all_models':
                        store.pause('decision_outcome_unknown',{'decision_id':decision['decision_id']})
                    m.record_conversation_turn(rid,kind='director',summary='服务重开后保留原只读请求身份；未知模型不重发，已知结果须显式核对。',status=m.runs[rid]['status'])
            for record in store.bindings():
                if record['state']=='prepared' and record['child_id'] not in m.runs and record.get('child_snapshot'):
                    snapshot=copy.deepcopy(record['child_snapshot'])
                    m.runs[record['child_id']]=snapshot
                    m.run_directories[record['child_id']]=m._projects().runs_root(project['id'])/record['child_id']
                    m._save(snapshot)
                if record['child_id'] in m.runs:
                    m.runs[record['child_id']]['director_delegation']=copy.deepcopy(record)

    def _action_execution(self, parent, store):
        """Original task facts, independent of an accepted human action."""
        unknown={'state':'unknown','code':'director_execution_unverified','outcome_unknown':True,'child_id':None}
        safe_codes={'invalid_judge_settings','director_child_creation_rejected','server_restarted',
                    'child_requires_review','director_decision_not_replayable','director_event_consumed',
                    'cancelled','director_continuation_unavailable','director_recovery_required'}
        def value(state, code=None, child=None):
            return {'state':state,'code':code if code in safe_codes else ('director_execution_failed' if code else None),
                    'outcome_unknown':state=='unknown','child_id':child}
        try:
            error=parent.get('error') or {}
            if error.get('outcome_unknown') or parent.get('director_ledger_unknown'): return unknown
            records=store.bindings(parent['id'])
            rejected=None
            for record in records:
                if record['state']=='unknown': return unknown
                if store.confirmed_not_created(record):
                    rejected=record
                    continue
                child=self.manager.runs.get(record['child_id'])
                if record['state']!='reserved' and (not child or child.get('project_id')!=parent['project_id']):
                    return unknown
                if child and ((child.get('error') or {}).get('outcome_unknown') or child.get('status')=='interrupted'):
                    return unknown
            if rejected:
                if any(record['state']=='started' for record in records): return unknown
                return value('failed',rejected['rejection']['code'])
            session=store.session(parent['id'])
            decision=store.decision_for(session['event_id']) if session else None
            worker=self.manager.workers.get(parent['id'])
            active=bool(worker and worker.is_alive())
            if decision and decision['state'] in {'started','unknown'} and not active: return unknown
            if parent['status']=='interrupted': return unknown
            if parent['status']=='completed': return value('completed')
            if parent['status'] in {'failed','cancelled'}: return value('failed',error.get('code') or 'cancelled')
            child_id=parent.get('active_child_id')
            if parent['status'] in {'awaiting_resume','waiting_child','queued'}:
                return value('waiting',child=child_id)
            if active: return value('running',child=child_id)
            if parent['status']=='running': return unknown
            return value('not_dispatched')
        except Exception:
            # Read failure does not erase acceptance or manufacture a result.
            return unknown

    def action_receipt(self, identifier, action_id, query):
        # _get also hydrates Working/resource projections. A broken projection
        # must not prevent a read of an independently committed action receipt.
        parent=self.manager.runs.get(identifier)
        if parent is None: reject('任务不存在。','not_found',404)
        if parent.get('operation')!='conversation' or parent.get('project_id')!=query['project_id']:
            reject('原动作父请求/项目身份不一致。','director_action_conflict',409)
        store=self.stores.get(parent['project_id'])
        if store is None:
            reject('原动作SQL关联尚不可确认。','director_legacy_recovery_unavailable',409)
        receipt=store.action_receipt(identifier,action_id,**query)
        execution=(self._action_execution(parent,store) if receipt['receipt_state']=='accepted' else
                   {'state':'unknown','code':None,'outcome_unknown':True,'child_id':None})
        return {'director_action':receipt,'execution':execution}

    def action(self, identifier, action, body):
        from .director_contract import human_action
        body=human_action(body,action)
        parent=self.manager.runs.get(identifier)
        if parent is None: reject('任务不存在。','not_found',404)
        if parent.get('operation')!='conversation': reject('恢复动作只属于可信Director父请求。','director_unbound',409)
        store=self.store(parent['project_id'])
        query={'project_id':parent['project_id'],'task_id':body['task_id'],'action':action}
        prior=store.action_receipt(identifier,body['action_id'],body=body,**query)
        if prior['receipt_state']!='not_found':
            # A duplicate reads the original receipt even if the task failed
            # later. It must not schedule again or clear an unknown boundary.
            if prior['receipt_state']=='accepted' and body.get('execution_policy_version')==2:
                self._start_natural_resume(parent,body['action_id'])
            result=self.action_receipt(identifier,body['action_id'],query)
            try: run=self.manager.get(identifier)
            except Exception: run=None
            return {**result,'director_action':{**prior,**result['director_action']},
                    'dispatch_state':'already_accepted' if prior['receipt_state']=='accepted' else 'receipt_unverifiable','run':run}
        try:
            result=self._perform_action(identifier,action,body)
        except Exception:
            receipt=store.action_receipt(identifier,body['action_id'],body=body,**query)
            if receipt['receipt_state']!='accepted': raise
            result={'director_action':receipt,'dispatch_state':'accepted_execution_unavailable','run':None}
        view=self.action_receipt(identifier,body['action_id'],query)
        return {**result,**view,'director_action':{**result['director_action'],**view['director_action']}}

    def _perform_action(self, identifier, action, body):
        from .director_contract import human_action
        m=self.manager; parent=m._get(identifier)
        if parent.get('operation')!='conversation': reject('恢复动作只属于可信Director父请求。','director_unbound',409)
        store=self.store(parent['project_id'])
        if not store.parent(identifier) or not store.session(identifier): reject('旧M1缺恢复claim，不能推断重发。','director_legacy_recovery_unavailable',409)
        if action in {'recover', 'resume'} and store.recovery_view(identifier)['state'] == 'child_creation_rejected':
            reject('原委派在施工创建前已明确拒绝；保留失败记录，请在修复后发送新请求。',
                   'director_child_creation_rejected', 409)
        from .director_request_scope import enabled as scoped_request
        if self.is_readonly(parent) and not scoped_request(self,parent):
            if action!='recover':reject('只读peer不能恢复自主权限或修改政策。','director_readonly_violation',409)
            session=store.session(identifier);decision=store.decision_for(session['event_id'])
            if not session.get('decision') and (not decision or decision['state'] not in {'returned','applied'}):
                reject('只读恢复仅消费已知结果；不重发原模型。','director_decision_not_replayable',409)
        body=human_action(body,action)
        boundary_pause=(store.task().get('pause') or {}).get('reason')=='decision_boundary_invalid'
        from .director_request_scope import pause_applies
        if action=='recover' and boundary_pause and scoped_request(self,parent) and pause_applies(self,parent):
            reject('旧暂停证据不足或仍有活动/未知操作；须核实原请求及费用回执，不能通过新请求解除。','director_legacy_pause_unverified',409)
        legacy_boundary=action=='recover' and boundary_pause and not scoped_request(self,parent)
        if legacy_boundary:
            with store.state._connection() as c:origin=store.legacy_boundary_origin(c,store.task())
            if origin!=identifier:reject('原边界身份或无副作用/已结算证据不足，不能重放或解除暂停。','director_legacy_pause_unverified',409)
            original=store.decision_for(store.session(identifier)['event_id'])
            if not original or original['state']!='returned':reject('旧边界响应不是已确认返回。','director_decision_not_replayable',409)
            assistant=self._private(parent).load(original.get('private_ref'),original)
            from .providers import _agent_calls
            _agent_calls(assistant.get('tool_calls'),director_arguments=True)
        proof=None
        if body.get('execution_policy_version')==2:
            from .director_request_scope import natural_resume_proof
            proof=natural_resume_proof(self,parent)
        receipt=store.human_action(identifier,action,body,natural_execution=proof)
        if action == 'recover':
            from .resource_parent_recovery import reconcile_local_failure_parent
            reconciled=reconcile_local_failure_parent(m,parent,action_id=body['action_id'])
            if reconciled:
                if parent['status']=='completed':
                    return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'reconciled_completed'}
                child=m.runs.get(parent.get('active_child_id'))
                if child and child['status']=='awaiting_resume':
                    return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'child_wait_retained'}
            elif ((parent.get('error') or {}).get('code')=='server_restarted'
                    and parent.get('active_child_id') and parent.get('agent_resource_base')):
                reject('父任务的中断分类尚未被原调用/写入证据核实，不能重新执行。','resource_parent_unverified',409)
        if proof:
            scheduled=self._start_natural_resume(parent,body['action_id'])
            return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'decision_scheduled' if scheduled else 'already_accepted'}
        if action=='policy' and store.session(identifier).get('approval_event_id')==digest({'parent':identifier,'approved_action':receipt['action_id']}):
            session=store.session(identifier);claim=store.decision_for(session['event_id']);worker=m.workers.get(identifier)
            known_unstarted=(session['state']=='scope_approved' and (not claim or claim['state']=='prepared')
                             and not (worker and worker.is_alive()))
            if not known_unstarted:
                return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'already_accepted'}
        if legacy_boundary:
            # Only this proven returned boundary is cleared; other unknowns never enter here.
            with store.state._write() as c:
                task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
                if (task.get('pause') or {}).get('pause_id')!=body['expected_pause_id']:
                    reject('原边界pause已变化。','director_action_stale',409)
                task['pause']=None;task['revision']=task.get('revision',1)+1
                c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))
            session=store.session(identifier);session['natural_recovery']=True;store.save_session(session)
        from .director_request_scope import approved
        if approved(self,parent,receipt):
            parent.update(status='running',stage='director_scope_approved',cancel_requested=False,error=None)
            m._save(parent)
            worker=m.workers.get(identifier)
            if not worker or not worker.is_alive():m._start(parent,self.execute)
            return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'decision_scheduled'}
        session=store.session(identifier)
        if (session.get('queue') or {}).get('state')=='accepted':
            from .director_request_scope import schedule
            schedule(self,parent['project_id'])
            return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'queue_retained'}
        worker=m.workers.get(identifier)
        if worker and worker.is_alive(): return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'running'}
        session=store.session(identifier)
        for record in store.bindings(identifier):
            if record.get('result'):
                event=store.finish(record,record['result'])
                if not store.event(event)['consumed']:
                    session=store.attach_result(identifier,event,record['result'])
        if session.get('decision'):
            self._repair_reply(identifier)
            return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'local_reply_repaired'}
        if session['state']=='waiting_child':
            try:
                self._native_exchange(parent,session)
            except Exception as error:
                if getattr(error,'code',None)=='director_continuation_unavailable': self._pause_continuation(parent)
                raise
            record=store.child(session['child_id'])
            if record['state']=='reserved':
                self._prepare_child(parent,record)
                record=store.child(record['child_id'])
            if record['state']=='prepared':
                parent.update(status='waiting_child',active_child_id=record['child_id'],error=None)
                m._save(parent); self._dispatch(record)
                return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'child_started'}
            child=m.get(record['child_id'])
            if child['status'] in {'completed','failed','interrupted','cancelled'} and (child.get('error') or {}).get('code')!='server_restarted':
                self.finished(child['id'])
            else:
                reject('已开始的子执行只核对；暂停草稿需原child的显式恢复入口。','director_child_not_replayable',409)
            return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'result_checked'}
        event=store.event(session['event_id']); decision=store.decision_for(event['event_id'])
        if decision and decision['state'] in {'started','unknown'}:
            if (store.task().get('pause') or {}).get('scope')!='all_models':
                store.pause('decision_outcome_unknown',{'decision_id':decision['decision_id']})
            reject('原决策已开始或结果未知，不重发收费请求。','director_decision_not_replayable',409)
        if decision and decision['state']=='applied':
            reject('原事件已应用，不重复决策。','director_event_consumed',409)
        if not decision or decision['state']=='prepared': store.admission(event['source'])
        parent.update(status='running',error=None,cancel_requested=False,
            expected_project_revision=m.get_project(parent['project_id'])['revision'])
        m._save(parent); m._start(parent,self.execute)
        return {'run':m.get(identifier),'director_action':receipt,'dispatch_state':'decision_scheduled'}

    def _start_natural_resume(self,parent,action_id):
        """POST retry may schedule only the original, positively unstarted event."""
        m=self.manager;store=self.store(parent['project_id'])
        with m.lock:
            session=store.session(parent['id']);proof=session.get('natural_execution') or {}
            decision=store.decision_for(session['event_id']);worker=m.workers.get(parent['id'])
            if (proof.get('action_id')!=action_id or session['state']!='natural_execution_ready'
                    or session['event_id']!=proof['event_id'] or store.event(session['event_id'])['consumed']
                    or decision and decision['state']!='prepared' or worker and worker.is_alive()):return False
            self._native_exchange(parent,session)
            from .director_request_scope import busy,pause_applies
            if busy(self,parent) or pause_applies(self,parent):return False
            project=m.get_project(parent['project_id']);scope=session['request_scope']
            if project.get('current_scene')!=scope['baseline'] or project['workspace']['working_version']!=scope['working_version']:
                reject('恢复事件尚未执行，但Working来源已经变化。','director_stale',409)
            parent.update(status='running',stage='director_natural_execution',error=None,cancel_requested=False)
            m._save(parent);m._start(parent,self.execute)
            return True
