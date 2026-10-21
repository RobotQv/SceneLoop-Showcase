"""Explicit human creative scope; model effects never enlarge this capability."""
import copy
import re
from .director_contract import reject
from .director_contract import digest
from .tool_correction import UnexecutedToolRejected


class ScopeEditRejected(ValueError, UnexecutedToolRejected):
    """Only check_operations' locked, pre-operation validation may issue this."""
    code = 'director_scope_denied'

    def __init__(self, targets, allowed):
        super().__init__('编辑超出原Director对象范围；本次编辑未执行，请依据允许对象重新生成参数。')
        self.details = {'boundary': 'director_scope_prewrite', 'execution': 'confirmed_not_executed',
            'rejected_targets': sorted(targets), 'allowed_object_ids': sorted(allowed),
            'instruction': '保留已成功的工具结果，仅重新规划被拒绝及未执行的调用；现有对象必须在冻结范围内，每批仍最多16个编辑操作。preserve文字不会增加对象权限。'}


def normalize(value, policy):
    result={}
    if 'creation' in value:
        body=value['creation']
        if (not isinstance(body,dict) or not {'enabled','allow_new_objects','stop_conditions'}<=set(body)
                or set(body)-{'enabled','allow_new_objects','stop_conditions','review_stage'}
                or body.get('review_stage','appearance') not in ('appearance','structure')
                or body['enabled'] is not True or body['allow_new_objects'] is not True
                or not isinstance(body['stop_conditions'],list) or not 1<=len(body['stop_conditions'])<=8
                or any(not isinstance(s,str) or not s.strip() or len(s)>1000 for s in body['stop_conditions'])
                or not policy or (value.get('request_protocol')!='natural_v2' and policy.get('autonomous') is not True) or not policy.get('total_saved_limit')
                or value.get('conversation_mode')=='read_only'):
            reject('持续创作需要明确新增对象、停止条件和有总保存上限的自主授权。','creation_authorization_required')
        result['creation']=copy.deepcopy(body)
    if 'references' in value:
        refs=value['references']
        if (not isinstance(refs,list) or not 1<=len(refs)<=2
                or any(not isinstance(r,dict) or set(r)!={'artifact_id','sha256','purpose'} for r in refs)
                or any(not re.fullmatch(r'[0-9a-f]{32}',str(r['artifact_id']))
                       or not re.fullmatch(r'[0-9a-f]{64}',str(r['sha256'])) or r['purpose'] not in ('scene','asset') for r in refs)
                or len({r['purpose'] for r in refs})!=len(refs)):
            reject('每个用途最多采用一张具有准确SHA的项目参考。','project_reference_invalid')
        result['references']=copy.deepcopy(refs)
    if 'local_resources' in value:
        resources=value['local_resources']
        if (not result.get('creation') or not isinstance(resources,dict)
                or not {'profile_id','max_generations','job_timeout_seconds','max_in_flight'} <= set(resources)
                or set(resources)-{'profile_id','max_generations','job_timeout_seconds','max_in_flight','current_stage_permission'}
                or not isinstance(resources['profile_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',resources['profile_id'])
                or type(resources['max_generations']) is not int or not 1<=resources['max_generations']<=12
                or type(resources['job_timeout_seconds']) is not int or not 60<=resources['job_timeout_seconds']<=3600
                or type(resources['max_in_flight']) is not int or resources['max_in_flight']!=1):
            reject('本地资源需要创作授权、已保存配置、1–12生成数、60–3600秒单作业窗口及单in-flight。','local_asset_authorization_invalid')
        if 'current_stage_permission' in resources:
            permit = resources['current_stage_permission']
            if (permit != {'scope':'first_builder_stage','generation_count':1}
                    or type(permit.get('generation_count')) is not int):
                reject('当轮许可只允许一次绑定首个施工阶段的一次本地生成。','asset_permission_invalid')
        result['local_resources']=copy.deepcopy(resources)
    return result


def freeze_request(manager, request):
    from .project_references import adopted
    if not request.get('creation') and 'references' not in request:return
    request['reference_snapshots']=adopted(manager,request['project_id'],request.get('references',[]))
    selected = request.get('local_resources',{}).get('profile_id') or (request.get('asset_generation') or {}).get('profile_id')
    if selected and request.get('creation'):
        repository=manager.profile_repository
        profile=next((p for p in repository.list() if p['id']==selected),None) if repository else None
        if (not profile or profile.get('kind')!='asset3d' or profile.get('mode')!='local_process'
                or profile.get('provider_id')!='hunyuan' or profile.get('model')!='hunyuan3d-2mini'):
            if request.get('local_resources'):
                reject('本地持续授权只允许已保存的local_process资产配置。','local_asset_configuration_conflict',409)
        else: request['local_profile_snapshot']=copy.deepcopy(profile)


def register(task, parent):
    if not parent.get('creation'):return
    approved={key:copy.deepcopy(parent.get(key)) for key in
              ('creation','references','reference_snapshots','local_resources','local_profile_snapshot')}
    approved['total_saved_limit']=parent['director_policy']['total_saved_limit']
    previous=task.get('creative_authorization')
    if previous:
        if previous['approved']!=approved:
            reject('既有创作授权不能通过新请求静默改写。','creation_authorization_conflict',409)
        return
    authority={'project_id':parent['project_id'],'task_id':task['task_id'],
               'parent_id':parent['id'],'source':'protected_user_request','approved':approved}
    authority['authorization_id']=digest(authority)
    task['creative_authorization']=authority


def authority(host, parent):
    store=host.store(parent['project_id']);task=store.task()
    value=(task or {}).get('creative_authorization')
    if not value:return None
    raw={k:v for k,v in value.items() if k!='authorization_id'}
    session=store.session(value['parent_id'])
    accepted=(session or {}).get('run',{})
    expected={key:copy.deepcopy(accepted.get(key)) for key in
              ('creation','references','reference_snapshots','local_resources','local_profile_snapshot')}
    expected['total_saved_limit']=(accepted.get('director_policy') or {}).get('total_saved_limit')
    if (value['authorization_id']!=digest(raw) or value['task_id']!=task['task_id']
            or value['project_id']!=parent['project_id'] or value['source']!='protected_user_request'
            or value['approved']!=expected or not accepted.get('creation')):
        reject('原可信创作授权或请求身份不可确认。','creation_authorization_conflict',409)
    return copy.deepcopy(value)


def local_authorization(host, run):
    """Asset Owner consumes this SQL-frozen authority; it owns atomic grant usage."""
    record=host.binding(run['id'],run.get('project_id')) if run.get('project_id') else None
    if not record:reject('本地资产授权需要原Director委派。','local_asset_authorization_required',409)
    parent=host.manager.runs[record['parent_id']]
    approved=authority(host,parent)
    if not approved or record['brief'].get('creation_authorization')!=approved:
        reject('委派没有原本地持续授权。','local_asset_authorization_required',409)
    body=approved['approved'];resources=body.get('local_resources')
    if not resources and body.get('local_profile_snapshot'):
        resources={'profile_id':body['local_profile_snapshot']['id'],'max_generations':0,'job_timeout_seconds':900,'max_in_flight':1}
    if not resources:reject('本任务未批准本地生成。','local_asset_authorization_required',409)
    intent=host.store(run['project_id']).task().get('local_asset_authorization_intent') or {}
    if intent.get('state')!='active' or intent.get('authorization_id')!=approved['authorization_id']:
        reject('本地授权尚未幂等补齐或已停止。','local_asset_authorization_not_active',409)
    if parent.get('cancel_requested') or host.store(run['project_id']).task().get('pause'):
        reject('本地持续任务已停止或暂停。','local_asset_authorization_stopped',409)
    return {**copy.deepcopy(resources),'project_id':approved['project_id'],'task_id':approved['task_id'],
            'authorization_id':approved['authorization_id'],'source_parent_id':approved['parent_id'],
            'profile_snapshot':copy.deepcopy(body['local_profile_snapshot']),
            'asset_reference':next((copy.deepcopy(r) for r in body['reference_snapshots'] if r['purpose']=='asset'),None)}


def tools(host, parent):
    from .director_contract import TOOLS
    result=copy.deepcopy(TOOLS)
    if authority(host,parent):
        for tool in result:
            function=tool['function'];properties=function['parameters']['properties']
            if function['name']=='delegate_task':
                properties['object_ids']['minItems']=0
                function['description']+=' Explicit creative authorization allows [] for bootstrap/new objects; existing targets must use actual IDs.'
            if function['name']=='director_goal_plan':
                properties['expected_scene']['type']=['object','null']
                properties['expected_working_version']['minimum']=0
    return result


def ensure_local_authority(host, parent):
    """Durable core intent -> idempotent asset SQL authority, never fresh credit."""
    import json
    approved=authority(host,parent)
    body=(approved or {}).get('approved') or {}
    if not body.get('local_profile_snapshot'):return None
    store=host.store(parent['project_id']);task=store.task()
    if parent.get('cancel_requested') or task.get('pause'):
        reject('任务已停止或暂停，不补发本地生成权限。','local_asset_authorization_stopped',409)
    resources=body.get('local_resources') or {'max_generations':0,'job_timeout_seconds':900}
    with store.state._write() as c:
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        intent=task.get('local_asset_authorization_intent')
        if not intent:
            intent={'authorization_id':approved['authorization_id'],'task_id':approved['task_id'],
                    'profile_snapshot':body['local_profile_snapshot'],'human_source':'protected_human_creation',
                    'limits':{key:resources[key] for key in ('max_generations','job_timeout_seconds')},'state':'pending'}
            intent['limits']['authorization_id']=approved['authorization_id']
            task['local_asset_authorization_intent']=intent
            c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))
        if intent['authorization_id']!=approved['authorization_id'] or intent['state'] in {'stopping','stopped'}:
            reject('原本地授权已停止或身份冲突。','local_asset_authorization_stopped',409)
        if intent['state']=='active':return {'task_id':approved['task_id'],'authorization_id':approved['authorization_id']}
    try:
        from .asset_local_authorization import LocalAssetAuthority
    except ImportError:
        reject('本地资产模块尚未安装；原授权意图已保留。','local_asset_module_unavailable',409)
    LocalAssetAuthority(store.state).authorize(intent['task_id'],intent['profile_snapshot'],intent['limits'],intent['human_source'])
    hook=getattr(host,'creation_checkpoint',None)
    if hook:hook('local_authority_fixed',parent)
    with store.state._write() as c:
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        current=task.get('local_asset_authorization_intent') or {}
        if task.get('pause') or parent.get('cancel_requested') or current.get('state') in {'stopping','stopped'}:
            reject('本地授权补齐期间任务已停止。','local_asset_authorization_stopped',409)
        if current.get('authorization_id')!=approved['authorization_id']:
            reject('本地授权意图身份冲突。','local_asset_authorization_required',409)
        current['state']='active'
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))
    return {'task_id':approved['task_id'],'authorization_id':approved['authorization_id']}


def stop_local_authority(host, parent):
    import json
    store=host.store(parent['project_id']);approved=authority(host,parent)
    if not (approved or {}).get('approved',{}).get('local_profile_snapshot'):return
    with store.state._write() as c:
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        intent=task.setdefault('local_asset_authorization_intent',{'authorization_id':approved['authorization_id'],
            'task_id':approved['task_id'],'human_source':'protected_human_creation'})
        intent['state']='stopping'
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))
    try:
        from .asset_local_authorization import LocalAssetAuthority
        LocalAssetAuthority(store.state).stop(approved['task_id'],approved['authorization_id'],'protected_human_creation')
    except Exception:
        # Persisted core revocation already blocks new local dispatch. A later
        # explicit retry repairs only this same stop intent; never authorize.
        return
    with store.state._write() as c:
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        task['local_asset_authorization_intent']['state']='stopped'
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))


def check_operations(host, run, rows, *, base_revision=None):
    record=host.binding(run['id'],run['project_id'])
    if not record:reject('原委派缺失。','director_scope_denied',409)
    brief=record['brief'];allowed=set(brief['object_ids'])
    parent=host.manager.runs[record['parent_id']]
    creative=authority(host,parent)
    creative=bool(creative and brief.get('creation_authorization')==creative)
    baseline={row['id'] for row in brief['baseline']['objects']}
    baseline.update(row['instance_id'] for row in brief['baseline'].get('assets',{}).get('instances',[]))
    def permitted(target):return target in allowed or creative and target not in baseline
    assets=None;denied=set()
    for row in rows:
        name=row.get('op')
        if name=='configure_lighting':
            if not creative:reject('全场灯光需要明确项目创作授权。','director_scope_denied',409)
            continue
        if name=='add' and creative:continue
        if name=='assign_surface':target=(row.get('target') or {}).get('object_id') or (row.get('target') or {}).get('instance_id')
        elif name=='asset_transform':target=row.get('instance_id')
        else:target=row.get('id') or row.get('object',{}).get('id')
        if name=='asset_transform' or name=='assign_surface' and (row.get('target') or {}).get('instance_id'):
            if assets is None:
                version=host.manager.scene_version(run['id'],base_revision,allow_active=True)
                assets={r['instance_id'] for r in version['plan'].get('assets',{}).get('instances',[])}
            if target not in assets:reject('资产目标没有登记在当前候选。','director_scope_denied',409)
        if not isinstance(target,str):
            reject('编辑超出原Director对象或创作范围。','director_scope_denied',409)
        if not permitted(target):denied.add(target)
    if denied:raise ScopeEditRejected(denied,allowed)


def public_authority(value):
    body=value['approved']
    return {key:copy.deepcopy(value[key]) for key in ('authorization_id','task_id','project_id')} | {
        'creation':{**copy.deepcopy(body['creation']),'review_stage':body['creation'].get('review_stage','appearance')},'total_saved_limit':body['total_saved_limit'],
        'local_resources':copy.deepcopy(body.get('local_resources')),
        'references':[{k:r[k] for k in ('artifact_id','sha256','purpose')} for r in body.get('reference_snapshots') or []]}


def select_stage(host, parent):
    """Original workspace metadata CAS; stage is requested scope, not a pass."""
    import json
    approved=authority(host,parent)
    if not approved:return
    stage=approved['approved']['creation'].get('review_stage','appearance')
    store=host.store(parent['project_id']);task=store.task()
    prior=task.get('creative_stage_selection')
    project=host.manager._projects().get(parent['project_id'],verify_artifacts=False)
    if prior:
        if prior['authorization_id']!=approved['authorization_id'] or project['workspace']['stage']!=stage:
            reject('原创作评价阶段已改变，请按原状态明确处理。','creation_stage_changed',409)
        return
    qualified=capability(host.manager,parent['project_id'])
    if not qualified['supported']:reject('当前原生能力尚不可确认，不能切换评价阶段。','creation_stage_unsupported',409)
    if project['workspace']['stage']!=stage:
        def update(workspace):workspace['stage']=stage;return workspace
        host.manager._projects().update_workspace(parent['project_id'],parent['expected_project_revision'],update)
    with store.state._write() as c:
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        task['creative_stage_selection']={'authorization_id':approved['authorization_id'],'stage':stage,
                                          'source':'protected_user_request','parent_id':approved['parent_id']}
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(json.dumps(task),))


def capability(manager, project_id):
    """Read frozen supported source facts; no renders, providers or model labels."""
    import json
    from .project_runtime import file_sha256
    from .scene_evidence import build_scene_evidence
    project=manager._projects().get(project_id,verify_artifacts=False)
    selected=project.get('current_scene')
    result={'schema_version':1,'project_id':project_id,'project_revision':project['revision'],
            'stage':project['workspace']['stage'],'stage_label':'外观（材质与灯光）' if project['workspace']['stage']=='appearance' else project['workspace']['stage'],
            'selection':copy.deepcopy(selected),'working_version':project['workspace']['working_version'],
            'supported':False,'reason_code':'source_unverified','self_contained_verified':False,
            'requires_explicit_creation':selected is None,'verified_features':{'surface':False,'assets':False}}
    if selected is None:
        result.update(supported=True,reason_code='empty_requires_explicit_creation')
        return result
    try:
        version=manager.scene_version(selected['run_id'],selected['revision_id'])
        plan=version['plan']
        if plan.get('scene_kind','parametric')!='parametric':
            result['reason_code']='scene_kind_unsupported';return result
        # Known typed embedded assets/procedural recipes do not imply arbitrary
        # declared libraries, paths, textures or dependencies are qualified.
        if any(key in plan and plan[key] not in ([],{}) for key in
               ('external_dependencies','libraries','textures','resource_paths')):
            result['reason_code']='external_dependencies_unqualified';return result
        validated=manager.blender.validate_plan(plan)
        for key in ('surface','assets'):
            if key in plan and (not isinstance(validated,dict) or validated.get(key)!=plan[key]):
                result['reason_code']='typed_resources_unsupported';return result
        measured=json.loads(version['files']['inspection'].read_text(encoding='utf-8'))
        evidence=build_scene_evidence(plan,measured)
        registered=plan.get('assets',{}).get('instances') or []
        if registered:
            checked=evidence.get('measured',{}).get('assets',[])
            projection=evidence.get('verification',{}).get('asset_projection') or {}
            if (projection.get('truncated') or len(checked)!=len(registered) or not all(v.get('available') is True for v in checked)
                    or {v['instance_id'] for v in checked}!={v['instance_id'] for v in registered}):
                result['reason_code']='asset_evidence_unverified';return result
        if 'surface' in plan:
            actual=measured.get('surface') or {}
            if (actual.get('verified') is not True or actual.get('state')!=plan['surface']
                    or actual.get('state_digest')!=digest(plan['surface'])):
                result['reason_code']='surface_evidence_unverified';return result
        result.update(supported=True,reason_code='native_self_contained_supported',self_contained_verified=True,
            evidence={key+'_sha256':file_sha256(version['files'][key]) for key in ('blend','plan','inspection')},
            verified_features={'surface':'surface' in plan,'assets':bool(plan.get('assets',{}).get('instances'))})
    except Exception as error:
        result['reason_code']=getattr(error,'code',None) or 'source_unverified'
    return result
