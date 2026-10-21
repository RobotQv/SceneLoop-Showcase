"""Human conversation admission and effect-only Director delegation contracts."""
import copy
import hashlib
import json
import re

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()

def reject(message, code='director_invalid', status=400):
    from .runtime import AppError
    raise AppError(message,code,status)

def normalize(value):
    from .runtime import _normalize_request_profiles, _normalize_request_judge
    allowed = {'operation','project_id','expected_project_revision','prompt','idempotency_key',
        'profiles','constraints','execution','iteration','judge','director_policy','conversation_mode','asset_generation','request_protocol',
        'creation','references','local_resources'}
    if not isinstance(value,dict) or set(value)-allowed:
        reject('对话请求仅接受可信用户入口的明确字段。')
    natural=value.get('request_protocol') in {'natural_v1','natural_v2'}
    managed=value.get('request_protocol')=='natural_v2'
    if 'request_protocol' in value and not natural:reject('未知请求协议。')
    if 'conversation_mode' in value and (not isinstance(value['conversation_mode'],str) or value['conversation_mode'] not in ({'auto','write','read_only'} if natural else {'write','read_only'})):
        reject('conversation_mode需明确为write或read_only。')
    if 'asset_generation' in value:
        asset=value['asset_generation']
        if (value.get('conversation_mode')=='read_only' or not isinstance(asset,dict)
            or set(asset)!={'enabled','profile_id'} or asset['enabled'] is not True
            or not isinstance(asset['profile_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',asset['profile_id'])):
            reject('资产能力仅限可信人类write入口的enabled/profile_id。','invalid_asset_generation')
    for key,pattern in (('project_id',r'[0-9a-f]{32}'),('idempotency_key',r'[A-Za-z0-9_-]{12,128}')):
        if not isinstance(value.get(key),str) or not re.fullmatch(pattern,value[key]): reject('对话身份无效。')
    if not isinstance(value.get('prompt'),str) or not 1<=len(value['prompt'].strip())<=16000:
        reject('对话内容需为1–16000字。')
    if type(value.get('expected_project_revision')) is not int or value['expected_project_revision']<1:
        reject('对话需要当前项目修订。')
    if value.get('execution',{'mode':'headless'}) != {'mode':'headless'}:
        reject('Director首阶段只支持Headless。')
    original = value.get('profiles')
    if not isinstance(original,dict) or set(original)-{'brain','builder'}: reject('需要已保存的大脑配置引用。')
    director = _normalize_request_profiles({'profiles':{'brain':original.get('brain')}},'edit')['brain']
    builder = _normalize_request_profiles({'profiles':{'brain':original.get('builder',original.get('brain'))}},'edit')['brain']
    inherit=natural and 'director_policy' not in value
    policy = value.get('director_policy',{'mode':'statistics_only','autonomous':False} if inherit else None)
    if not isinstance(policy,dict) or type(policy.get('autonomous')) is not bool:
        reject('需明确设置持续自主权限及费用政策。','director_policy_required')
    if policy.get('mode') == 'statistics_only':
        if set(policy)-{'mode','autonomous','total_saved_limit'}: reject('仅统计政策字段无效。')
    elif policy.get('mode') == 'strict':
        if (not {'mode','autonomous','currency','limit'}<=set(policy)
                or set(policy)-{'mode','autonomous','currency','limit','total_saved_limit'} or policy.get('currency') not in {'CNY','USD'}
                or type(policy['limit']) not in {int,float} or not 0<policy['limit']<=100000): reject('严格政策字段无效。')
    else: reject('需明确仅统计授权；缺可靠价格的严格政策会暂停。')
    if 'total_saved_limit' in policy and (type(policy['total_saved_limit']) is not int or not 1<=policy['total_saved_limit']<=100000):
        reject('显式上层正式保存总授权无效。')
    constraints = value.get('constraints',{})
    if (not isinstance(constraints,dict) or set(constraints)-{'style','preserve','key_objects_recognizable'}
        or any(type(constraints.get(k,'')) is not str or len(constraints.get(k,''))>4000 for k in ('style','preserve'))
        or constraints.get('key_objects_recognizable',True) is not True): reject('项目约束无效。')
    iteration = value.get('iteration',{'mode':'local_round_v1','enabled':True,'max_rounds':3})
    if (not isinstance(iteration,dict) or set(iteration)!= {'mode','enabled','max_rounds'}
            or iteration['mode']!='local_round_v1' or iteration['enabled'] is not True
            or type(iteration['max_rounds']) is not int or not 1<=iteration['max_rounds']<=12): reject('施工保存目标需为1–12。')
    judge = value.get('judge',{'enabled':True,'mode':'follow_brain','trigger_rounds':3})
    if (not isinstance(judge,dict) or set(judge)-{'enabled','mode','profile','reasoning_effort','trigger_rounds'}
            or judge.get('enabled') is not True or judge.get('mode') not in ('follow_brain','profile')
            or type(judge.get('trigger_rounds')) is not int or not 1<=judge['trigger_rounds']<=3): reject('需保留独立Judge及1–3保存触发。')
    if judge.get('reasoning_effort') is not None and not isinstance(judge['reasoning_effort'],str):
        reject('裁判思考设置需为原枚举或null。','invalid_judge_settings')
    if judge['mode']=='follow_brain' and judge.get('profile') is not None:
        reject('随Builder裁判不能另带独立配置。','invalid_judge_settings')
    if judge['mode']=='profile' or 'reasoning_effort' in judge or 'profile' in judge:
        profile=judge.get('profile')
        if judge['mode']=='profile' and (not isinstance(profile,dict) or not profile.get('id')
            or type(profile.get('revision')) is not int or profile['revision']<1):
            reject('裁判需要已保存的脑配置引用和有效修订。','configuration_required')
        judge=_normalize_request_judge({'judge':judge},'edit',value['project_id'],iteration,iteration['mode'])
        # Conversation configuration selects the reviewer, not the yet-unknown
        # child's execution policy. Only a new child freezes derived limits.
        judge.pop('max_calls', None)
        if judge['mode']=='follow_brain' and judge['reasoning_effort'] is None:
            # Explicit null UI defaults are equivalent to the historical compact request.
            judge={key:judge[key] for key in ('enabled','mode','trigger_rounds')}
    from .director_creation import normalize as normalize_creation
    capabilities=normalize_creation(value,({**policy,'total_saved_limit':policy.get('total_saved_limit',iteration['max_rounds'])} if managed else policy))
    return {**capabilities,**{k:copy.deepcopy(value[k]) for k in ('project_id','expected_project_revision','idempotency_key')},
        **({'request_protocol':value['request_protocol'],'conversation_mode':value.get('conversation_mode','auto')} if natural else
           {'conversation_mode':value['conversation_mode']} if 'conversation_mode' in value else {}),
        **({'asset_generation':copy.deepcopy(value['asset_generation'])} if 'asset_generation' in value else {}),
        'operation':'conversation','prompt':value['prompt'].strip(),'profiles':{'brain':director,'builder':builder},
        'constraints':copy.deepcopy(constraints),'explicit_constraints':copy.deepcopy(constraints),
        'execution':{'mode':'headless'},'evaluation':{'mode':'reference' if any(r['purpose']=='scene' for r in capabilities.get('references',[])) else 'intent'},'iteration':copy.deepcopy(iteration),
        'judge':copy.deepcopy(judge),'director_policy':None if inherit else copy.deepcopy(policy)}

def delegation(value, *, allow_empty_objects=False):
    if not isinstance(value,dict) or set(value)!= {'goal','criteria','object_ids','item_ids','preserve'}:
        reject('delegate_task: 必须且仅传goal、criteria、object_ids、item_ids、preserve；不接受施工脚本。')
    for key in ('goal','preserve'):
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>4000: reject(key+': 必须为非空文字，最多4000字。')
    for key in ('criteria','object_ids','item_ids'):
        if (not isinstance(value[key],list) or len(value[key])<(0 if key=='object_ids' and allow_empty_objects else 1)
            or key!='object_ids' and len(value[key])>16
            or any(not isinstance(x,str) or not x.strip() or len(x)>1000 for x in value[key])
            or len(set(value[key]))!=len(value[key])):
            hint='；从当前Plan复制已有item_id，若Plan为空先用director_goal_plan的update.changes初始化工作项，不能传[]' if key=='item_ids' else ''
            count='当前已授权对象集合内' if key=='object_ids' else '1–16个'
            reject(f'{key}: 需要{count}不重复的非空字符串，每项最多1000字'+hint+'。')
    for key in ('item_ids','object_ids'):
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',x) for x in value[key]):reject(key+': 每个ID须为1–80个字母、数字、下划线或连字符，复制已有身份。')
    return copy.deepcopy(value)

def tool(name, description, properties, required):
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}

STR = {'type':'string'}
LIST = {'type':'array','items':STR}
LOCAL_IDS={'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,
           'items':{'type':'string','minLength':1,'maxLength':80,'pattern':'^[A-Za-z0-9_-]{1,80}$'}}
PLAN_TEXT={'type':'string','maxLength':4000}
PLAN_REFS={'type':'array','maxItems':32,'uniqueItems':True,
           'items':{'type':'string','minLength':1,'maxLength':2000,'pattern':r'\S'}}
PLAN_UPDATE={'type':'object','additionalProperties':False,
    'description':'Use changes (not items), and focus_item_id (not focus). Server supplies update_id/expected_version; do not include them here.',
    'properties':{
        'changes':{'type':'array','maxItems':32,'description':'For a NEW item supply set.desired, set.basis and nonempty set.criteria; use actual object/Issue IDs. Existing items accept partial set updates.',
            'items':{'type':'object','additionalProperties':False,'required':['item_id'],
                'properties':{'item_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,80}$'},
                    'set':{'type':'object','additionalProperties':False,'properties':{
                        **{k:copy.deepcopy(PLAN_TEXT) for k in ('title','desired','basis')},
                        **{k:copy.deepcopy(PLAN_REFS) for k in ('criteria','object_ids','issue_ids')},
                        'status':{'type':'string','enum':['planned','active','blocked','parked']}}},
                    'observations':{'type':'array','maxItems':0,'items':{'type':'object'},'description':'Omit this field. Director cannot append execution or quality observations.'}}}},
        'order':{'type':'array','maxItems':128,'uniqueItems':True,'items':{'type':'string'},'description':'Existing/current Plan item IDs in desired order.'},
        'focus_item_id':{'type':['string','null'],'description':'A Plan item_id, or null; this is not a field named focus.'}}}
GOAL_REF={'type':['object','null'],'additionalProperties':False,'required':['id','version','content_digest'],
    'description':'Copy current facts.goal id/version/content_digest exactly; null only when facts.goal is null.',
    'properties':{'id':{'type':'string','pattern':'^[0-9a-f]{32}$'},'version':{'type':'integer','minimum':1},
                  'content_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'}}}
SCENE_REF={'type':'object','additionalProperties':False,'required':['run_id','revision_id'],
    'description':'Copy facts.working.selection run_id/revision_id exactly, without scene_id/title.',
    'properties':{'run_id':{'type':'string','pattern':'^[0-9a-f]{32}$'},
                  'revision_id':{'type':['string','null'],'pattern':'^r[0-9]{3,6}$'}}}
TOOLS = [tool('delegate_task','Delegate one local effect; Builder chooses parameters/tools. item_ids must be a NONEMPTY list of existing Plan item IDs. If Plan has no suitable items, first initialize them with director_goal_plan(update.changes), consume that result in new reasoning, then delegate. Do not invent an empty scope.',
    {'goal':{'type':'string','minLength':1,'maxLength':4000,'pattern':r'\S'},
     'criteria':{'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,'items':{'type':'string','minLength':1,'maxLength':1000,'pattern':r'\S'}},
     'object_ids':{**{k:copy.deepcopy(v) for k,v in LOCAL_IDS.items() if k!='maxItems'},
         'description':'Existing object IDs within the current request scope. This delegation scope is separate from the unchanged maximum of 16 edit operations per apply_edit batch.'},
     'item_ids':{**copy.deepcopy(LOCAL_IDS),'description':'Copy item IDs from current facts.plan.items; [] is invalid. Initialize missing Plan items first.'},
     'preserve':{'type':'string','minLength':1,'maxLength':4000,'pattern':r'\S'}},
    ['goal','criteria','object_ids','item_ids','preserve']),
    tool('director_reply','The final reply ends this request; it does not schedule later construction. Report actual results/progress, an evidenced no-op, blocker, clarification need or failure. A queued/approval reply only reports the existing pending state. Put result_id/working_version in their structured fields when consuming a child result; do not fill the human summary with internal IDs or default accounting diagnostics.',
    {'summary':{'type':'string','minLength':1,'maxLength':4000,'description':'User-facing actual result/progress and remaining issues, in plain language.'},
     'next_focus':{'type':'string','minLength':1,'maxLength':4000,'description':'Informational remaining focus; this does not schedule or execute work. Do not use it to defer an admitted edit as if it would run after this final reply.'},'result_id':STR,'working_version':{'type':'integer'}},['summary','next_focus']),
    tool('director_observe','Read the actual current Working in appearance or neutral structure mode. A view is main/auxiliary preset or position+target+lens/roll_degrees. No scene edits. Images follow in the next dynamic context; tool result contains bounded references only.',
         {'view':{'type':'object','properties':{'preset':STR,'mode':STR,'position':{'type':'array','items':{'type':'number'}},
                  'target':{'type':'array','items':{'type':'number'}},'lens':{'type':'number'},'roll_degrees':{'type':'number'}}}},['view']),
    tool('director_goal_plan','At an idle construction boundary continue the existing Goal or adopt only this accepted user request. CAS on actual Goal/Plan/Working. update permits global intent, items, order and focus; no scene save or invented quality. Never send your own global goal text. Initialize authorized items before delegation.',
         {'goal_mode':{'type':'string','enum':['continue','adopt_user_request']},
          'expected_goal_ref':GOAL_REF,'expected_plan_version':{'type':'integer','minimum':0,'description':'Copy facts.plan.version; 0 only when no Plan exists.'},
          'expected_scene':SCENE_REF,'expected_working_version':{'type':'integer','minimum':1,'description':'Copy facts.working.working_version.'},'update':PLAN_UPDATE},
         ['goal_mode','expected_goal_ref','expected_plan_version','expected_scene','expected_working_version','update'])]
READONLY_TOOLS=[copy.deepcopy(TOOLS[1]),{**copy.deepcopy(TOOLS[1]),'function':{
    **copy.deepcopy(TOOLS[1]['function']),'name':'director_clarify',
    'description':'Ask for clarification in a read-only reply; no execution or authority change.'}}]
SYSTEM = '''You are the Director. Read actual Working images and authoritative scene facts.
Decide local effects, preservation and acceptance criteria; the Builder independently chooses geometry, dimensions and tool order.
Use delegate_task for authorized construction. Use director_reply for the final response after actual results, discussion, clarification or an evidenced reason to end without edits. Its next_focus is informational and never schedules construction.
Use director_observe for additional real evidence; structure is neutral shape inspection, not material/light quality. Compare only identical mode/profile/camera/settings. Repeated identical observations without new evidence may pause autonomy.
Use director_goal_plan only at an idle construction boundary. Continue preserves the current Goal; adopt_user_request takes the accepted user request and inherited design/constraints, never a model-written global goal. Plan metadata is not a scene save or quality pass. Authorized criteria are frozen for the Builder.
Builder self-check, actual saved state and independent Judge are separate facts. Never invent a commit or quality pass.
After a child result, examine its real pictures and receipts, then cite result_id and working_version in your new decision.
Internal identities belong in structured tool fields, not a recital in the user-facing summary.
Do not retry unknown writes, reset task identity/costs, choose provider credentials, or bypass human policy.'''

def human_action(value, action):
    allowed={'action_id','task_id','expected_task_version','expected_policy_version','expected_pause_id','basis'}
    if action=='policy': allowed.add('policy')
    if action=='resume' and isinstance(value,dict) and 'execution_policy_version' in value:
        if type(value['execution_policy_version']) is not int or value['execution_policy_version']!=2:
            reject('继续执行政策版本无效。')
        allowed.add('execution_policy_version')
    if not isinstance(value,dict) or set(value)!=allowed: reject('恢复需要原task/pause/政策版本及明确人类依据。')
    for key in ('action_id','task_id'):
        if not isinstance(value[key],str) or not re.fullmatch(r'[0-9a-f]{32}',value[key]): reject('人类动作身份无效。')
    for key in ('expected_task_version','expected_policy_version'):
        if type(value[key]) is not int or value[key]<1: reject('人类动作版本无效。')
    if value['expected_pause_id'] is not None and (not isinstance(value['expected_pause_id'],str)
        or not re.fullmatch(r'[0-9a-f]{32}',value['expected_pause_id'])): reject('pause身份无效。')
    if not isinstance(value['basis'],str) or not 1<=len(value['basis'].strip())<=2000: reject('需要有据的人类恢复说明。')
    if action=='policy':
        policy=value['policy']
        if not isinstance(policy,dict) or type(policy.get('autonomous')) is not bool:
            reject('需明确人类费用政策。')
        if policy.get('mode')=='statistics_only':
            if set(policy)-{'mode','autonomous','total_saved_limit','continuous_scope'}: reject('仅统计政策字段无效。')
        elif policy.get('mode')=='strict':
            if (not {'mode','autonomous','currency','limit'}<=set(policy)
                or set(policy)-{'mode','autonomous','currency','limit','total_saved_limit','continuous_scope'}
                or policy.get('currency') not in {'CNY','USD'} or type(policy['limit']) not in {int,float}
                or not 0<policy['limit']<=100000): reject('严格政策字段无效。')
        else: reject('政策模式无效；政策本身不能证明可信上界。')
        if 'total_saved_limit' in policy and (type(policy['total_saved_limit']) is not int or not 1<=policy['total_saved_limit']<=100000):
            reject('显式总保存授权无效。')
        if 'continuous_scope' in policy:
            grant=policy['continuous_scope']
            if (policy['autonomous'] is not True or not isinstance(grant,dict) or set(grant)!={'proposal_id','scope_digest'}
                or not isinstance(grant['proposal_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',grant['proposal_id'])
                or not isinstance(grant['scope_digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',grant['scope_digest'])):
                reject('持续批准必须引用原提案身份/范围摘要。')
    return copy.deepcopy(value)
