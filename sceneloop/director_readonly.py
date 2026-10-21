"""Narrow peer capability/progress snapshot; never renders or grants write authority."""
import copy
from decimal import Decimal
from .director_contract import reject

def _asset_history(state, child, setting, local_authorization=None):
    """Only original SQL facts; never constructs a Host or validates credentials."""
    import uuid
    from .asset_run_store import AssetRunStore
    from .asset_job_store import AssetJobStore
    from .asset_provider import AssetJobError
    from .asset_price import request_policy, public_approval, public_price, actual_usage_estimate
    assets=AssetRunStore(state);cap=assets.capability(child['id'])
    if not cap or cap['profile']['id']!=setting['profile_id']:
        raise AssetJobError('asset_capability_integrity')
    doc=assets.request(child['id'])
    if doc is None:
        # The original one-slot protocol fixes this key even before Job binding.
        request_id=uuid.uuid5(uuid.UUID(state.project_id),f"{child['id']}:asset-slot:1").hex
        key=uuid.uuid5(uuid.UUID(request_id),'submission').hex
        if child.get('asset_request') or AssetJobStore(state).by_key(key):
            raise AssetJobError('asset_job_binding_conflict')
        return None,None
    expected_id=uuid.uuid5(uuid.UUID(state.project_id),f"{child['id']}:asset-slot:1").hex
    from .local_asset_failure import verified_attempt_identity
    if (doc['run_id']!=child['id'] or doc['project_id']!=state.project_id
        or doc['slot']!=1 or not verified_attempt_identity(state,doc,expected_id)
        or doc['request']['profile']!=cap['profile'] or doc['credential_binding']!=cap['credential_binding']):
        raise AssetJobError('asset_request_sql_integrity')
    continuation=assets.continuation(doc['id'])
    phase=continuation['state'] if continuation else 'blocked' if doc.get('driver_error') else 'waiting'
    public={k:copy.deepcopy(doc[k]) for k in ('id','project_id','run_id','version','phase','closed','job_id')}
    local=cap['profile'].get('mode')=='local_process'
    if local and local_authorization:
        if (child.get('local_asset_authorization')!={key:local_authorization[key] for key in ('task_id','authorization_id')}
                or cap['profile']['id']!=local_authorization['profile_id']):
            raise AssetJobError('asset_local_authorization_conflict')
    public.update(price=({'policy':'local_task_authorization','supplier_hard_cap':False} if local else public_price(doc)),
        estimate=({'status':'not_applicable','credits':None,'unit':'credits','policy':'local_task_authorization',
                   'supplier_quote_required':False} if local else actual_usage_estimate() if request_policy(doc) == 'actual_usage' else
                  {'status':'unavailable','credits':None,'reason_code':'read_only_history_no_current_quote'}),
        approval=({'state':'closed' if doc['closed'] else 'approved' if doc['grant_id'] else 'pending',
                   'approval_id':doc['grant_id'],'max_credits':None,'policy':'local_task_authorization'} if local else public_approval(doc)),
        continuation={'state':phase,'resume_id':continuation['resume_id'] if continuation else None,
                      'blocked_reason':doc.get('driver_error')})
    if local:
        public['estimate']={'status':'not_applicable','credits':None,'unit':'credits','policy':'local_task_authorization',
                            'supplier_quote_required':False}
        public['approval'].update(policy='local_task_and_stage_authorization',requires_current_stage_permission=True,
            authorization_state=(local_authorization or {}).get('state','unavailable'))
        public['stage_permission']=copy.deepcopy(doc.get('stage_permission'))
    job=AssetJobStore(state).read(doc['job_id']) if doc['job_id'] else None
    if job and job['preparation']=='ready':
        public['specification']={k:copy.deepcopy(job['raw_artifact'][k]) for k in ('sha256','byte_size','contract') if k in job['raw_artifact']}
    return public,job

def asset_facts(manager, project_id, task_id):
    """Project/task/child-bound Job facts, credits never converted to model units."""
    store=manager._director_host().stores.get(project_id);items=[];seen=set();buckets={}
    local=None; permissions=[]
    if store and (store.task() or {}).get('creative_authorization'):
        task=store.task();approved=task['creative_authorization'];source=(store.session(approved['parent_id']) or {}).get('run')
        if source:
            from .director_creation import authority
            original=authority(manager._director_host(),source)
            resources=original['approved'].get('local_resources')
            if not resources and original['approved'].get('local_profile_snapshot'):
                resources={'profile_id':original['approved']['local_profile_snapshot']['id'],'max_generations':0,'job_timeout_seconds':900,'max_in_flight':1}
            if resources:
                intent=task.get('local_asset_authorization_intent') or {}
                local={**copy.deepcopy(resources),'task_id':task['task_id'],'authorization_id':original['authorization_id'],
                       'state':intent.get('state','approved_pending_activation'),
                       'requires_current_stage_permission':True,'task_quota_is_permission':False,'supplier_quote_required':False}
    for record in store.bindings() if store else ():
        parent=store.parent(record['parent_id'])
        if record['task_id']!=task_id or not parent or parent['task_id']!=task_id:
            reject('资产关联跨Director任务。','director_asset_scope_invalid',409)
        if not record.get('request',{}).get('asset_generation'):continue
        child=manager.runs.get(record['child_id'])
        if not child:
            if record['state']=='reserved' or store.confirmed_not_created(record):continue
            reject('已绑定资产子执行缺失，原身份不可确认。','director_asset_scope_invalid',409)
        if child.get('project_id')!=project_id:reject('资产子执行跨项目。','director_asset_scope_invalid',409)
        if local:
            from .agent_resource_store import ResourceStore
            from .asset_run_store import AssetRunStore
            request=ResourceStore(store.state).request(run_id=child['id'])
            cap=AssetRunStore(store.state).capability(child['id'])
            if request and request.get('kind')=='local_asset_permission':
                if request['task_id']!=task_id or request['project_id']!=project_id:
                    reject('本地许可跨任务。','director_asset_scope_invalid',409)
                permissions.append({'child_id':child['id'],'request_id':request['id'],
                    'status':request['status'],'permission':copy.deepcopy(request['permission']),
                    'source':'resource_request','current_admission_not_implied':True})
            elif (cap or {}).get('initial_permission'):
                permissions.append({'child_id':child['id'],'status':'bound_first_stage',
                    'permission':copy.deepcopy(cap['initial_permission']),
                    'source':'protected_user_request','current_admission_not_implied':True})
        from .asset_provider import AssetJobError
        try:public,job=_asset_history(store.state,child,record['request']['asset_generation'],local)
        except AssetJobError as error:reject('原SQL资产事实不可确认：'+error.code,error.code,409)
        if not public:continue
        if public['run_id']!=child['id'] or public['project_id']!=project_id:
            reject('资产请求身份不一致。','director_asset_scope_invalid',409)
        item={'parent_id':record['parent_id'],'child_id':child['id'],
              **{key:copy.deepcopy(public.get(key)) for key in ('id','phase','closed','job_id','price','estimate','approval','continuation','specification','stage_permission')}}
        if public['job_id']:
            item['job']={key:copy.deepcopy(job[key]) for key in ('job_id','version','dispatch','remote','local','preparation',
                'creation_attempts','cost','credits')}
            item['cost_evidence']=copy.deepcopy(job['cost_history'][-1:])
            if job['job_id'] not in seen:
                seen.add(job['job_id']);bucket=buckets.setdefault(job['cost'],{'known_amount':'0','known_jobs':0,'unknown_jobs':0})
                if job['credits'] is None:bucket['unknown_jobs']+=1
                else:
                    bucket['known_amount']=str(Decimal(bucket['known_amount'])+Decimal(str(job['credits'])))
                    bucket['known_jobs']+=1
        items.append(item)
    result={'requests':items[-16:],'omitted_requests':max(0,len(items)-16),'job_count':len(seen),
        'units':{'credits':buckets},'converted_to_model_currency':False,
        'execution':{'status':'unavailable' if manager.asset_host is None else 'not_verified',
                     'reason_code':'asset_host_missing' if manager.asset_host is None else 'requires_current_configuration_validation'},
        'boundary':'Independent explicit human approval per child Job; catalog/balance are not consumption or a supplier hard cap.'}
    if local:
        local['stage_permissions']=permissions[-16:]
        result['local_authorization']=local
        result['execution']={'status':'available_requires_stage_permission' if local['state']=='active' else local['state'],
            'reason_code':'local_task_quota_separate_from_stage_permission','requires_current_stage_permission':True,'supplier_quote_required':False,
            'runtime_available':manager.asset_host is not None,'requires_current_configuration_validation':True}
        result['boundary']='Local Hunyuan is available in bootstrap and local rounds. Each generation requires both task quota AND current-stage permission. The initial explicit first-stage permission binds only once; otherwise Builder requests permission and resumes the same Run after approval or denial. Cloud Jobs require separate approval.'
    return result

def enabled(store, identifier):
    session=store.session(identifier)
    if not session:return False
    from .director_request_scope import readonly_session
    return readonly_session(session)

def progress(manager, project_id):
    store=manager._director_host().store(project_id)
    mains=[run for run in manager.runs.values() if run.get('project_id')==project_id
           and run.get('operation')=='conversation' and not enabled(store,run['id'])]
    active=[run for run in mains if run['status'] not in {'completed','failed','cancelled','interrupted'}]
    main=max(active or mains,key=lambda row:row['created_at'],default=None)
    writers=[run for run in manager.runs.values() if run.get('project_id')==project_id
             and run.get('operation')!='conversation' and run['status'] not in {'completed','failed','cancelled','interrupted'}]
    fallback=max(writers,key=lambda row:row['created_at'],default=None)
    bound=store.child(main['active_child_id']) if main and main.get('active_child_id') else None
    delegated=bool(bound and bound['state'] in {'reserved','prepared','started','waiting'})
    child=manager.get(bound['child_id']) if delegated else manager.get(fallback['id']) if fallback else manager.get(bound['child_id']) if bound and bound['child_id'] in manager.runs else None
    def summary(run):
        if not run:return None
        return {key:copy.deepcopy(run.get(key)) for key in ('id','status','stage','cancel_requested')}
    state=manager._projects().state_store(project_id)
    return {'main':summary(main),'child':summary(child),
        'child_relationship':'delegated_child' if child and bound and child['id']==bound['child_id'] else 'existing_scene_run' if child else None,
        'saved_edit_rounds':state.readiness(child['id'])['saved_edit_rounds'] if child else 0,
        'draft_attempts':state.local_edit_attempt_count(child['id']) if child else 0,
        'waiting_reason':(child or main or {}).get('stage'),
        'asset_wait':{key:copy.deepcopy((child.get('asset_request') or {}).get(key))
                      for key in ('id','phase','closed','job_id')} if child and child.get('asset_request') else None,
        'recent_events':[{key:copy.deepcopy(row.get(key)) for key in ('seq','time','stage','message')}
                         for row in (child or main or {}).get('events',[])[-4:]]}

def attach(manager, parent, facts):
    from .runtime import now
    state=progress(manager,parent['project_id']);view=facts['working']
    capability={'mode':'read_only','source':'protected_user_request',
                'allowed_tools':['director_reply','director_clarify'],'writes':False,'renders':False}
    from .director_request_scope import enabled,tools
    host=manager._director_host()
    if enabled(host,parent):
        names=[row['function']['name'] for row in tools(host,parent)]
        capability.update(source='accepted_request_scope',allowed_tools=names,renders='director_observe' in names,
                          permission_scope='current_request_only')
    facts.update(capability=capability,
                 progress=state,as_of={'at':now(),'project_id':parent['project_id'],'task_id':facts['task_id'],
                    'working':{key:copy.deepcopy(view.get(key)) for key in
                        ('selection','working_version','blend_sha256','plan_sha256','images')},
                    'main':state['main'],'child':state['child']})

def blocked_reply(facts, reason):
    return {'summary':'仅只读状态说明：Working Version '+str(facts['working']['working_version'])+
        '；主请求/子执行状态见读取时点记录。费用或未知请求边界阻止模型，本次未调用模型，未实施修改。',
        'next_focus':'由原主请求的可信人类入口核实暂停；普通问话不恢复施工。',
        'working_version':facts['working']['working_version'],'as_of':copy.deepcopy(facts['as_of']),
        'read_only':True,'source':'runtime_status','blocking_reason':reason,'model_called':False}
