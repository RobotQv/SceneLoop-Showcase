"""M3a metadata-only Goal/Plan boundary using existing project SQL authority."""
import copy
import json
import uuid
from .director_contract import digest, reject
from .refinement_plan import apply_update, discussion_request, goal_content, selection_identity, validate_update

PROTECTED = ('title','desired','basis','criteria','object_ids')

def reference(goal): return {k:goal[k] for k in ('id','version','content_digest')} if goal else None
def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def semantic_state(goal, plan, project, claim, session):
    """Bounded trusted facts; formal labels/IDs/order/focus never imply progress."""
    intentions=sorted({digest({'desired':row['desired'],'criteria':sorted(row['criteria']),
                              'object_ids':sorted(row['object_ids'])})
                       for row in (plan or {}).get('items',[])})
    working=claim['input']['working']
    pictures=sorted({row['sha256'] for row in working.get('images',[]) if row.get('sha256')})
    evidence=sorted({row['identity'] for row in session.get('observations',[]) if row.get('identity')})
    issues=sorted({digest({key:row.get(key) for key in ('id','object_ids','priority','status','source','review')})
                   for row in project['workspace'].get('issues',[])})
    facts={'goal_intent':digest(goal['content']) if goal else None,'plan_intents':intentions,
           'scene':{key:working.get(key) for key in ('blend_sha256','plan_sha256')},
           'pictures':pictures,'credible_observations':evidence,'effective_issues':issues}
    return {'digest':digest(facts),'facts':facts}

def metadata_progress(c, task, metadata, decision_id):
    """Apply exactly once with the original native decision, across all parents."""
    state=metadata.get('semantic_progress')
    segment=task.setdefault('metadata_progress',{'consecutive_no_change':0,'confirmed_results':0,
                                               'recent_receipts':[],'scope':{'task_id':task['task_id']}})
    if not state:
        return {'reason':'metadata_progress_unverifiable','scope':segment['scope'],
                'receipt_refs':[metadata['metadata_id']],'meaning':'legacy outcome retained; no invented progress proof'}
    before=state['before']; after=state['after']; seen=set()
    for row in c.execute("SELECT m.document,d.document FROM director_metadata m JOIN director_decisions d ON d.decision_id=m.decision_id WHERE d.decision_id!=?",(decision_id,)):
        if json.loads(row[1])['state']!='applied': continue
        prior=json.loads(row[0]).get('semantic_progress')
        if prior: seen.update((prior['before']['digest'],prior['after']['digest']))
    if not seen: seen.add(before['digest'])
    progressed=after['digest'] not in seen
    segment['confirmed_results']+=1
    segment['consecutive_no_change']=0 if progressed else segment['consecutive_no_change']+1
    segment['recent_receipts']=(segment['recent_receipts']+[metadata['metadata_id']])[-4:]
    segment.update(last_before=before,last_after=after,last_action=state['attempted'],last_progress=progressed)
    if segment['consecutive_no_change']<2: return None
    return {'reason':'metadata_no_new_state','scope':segment['scope'],
            'consecutive_no_change':segment['consecutive_no_change'],
            'before':before,'after':after,'attempted':state['attempted'],
            'receipt_refs':segment['recent_receipts'],
            'meaning':'two confirmed metadata results with no new substantive state; original results/native/costs retained'}

def validate(body, op_id, *, allow_empty_baseline=False):
    fields={'goal_mode','expected_goal_ref','expected_plan_version','expected_scene','expected_working_version','update'}
    def invalid(message):reject(message,'director_metadata_invalid',409)
    if not isinstance(body,dict) or set(body)!=fields:
        invalid('director_goal_plan: 必须且仅传goal_mode、expected_goal_ref、expected_plan_version、expected_scene、expected_working_version、update。CAS值复制当前facts。')
    if body['goal_mode'] not in ('continue','adopt_user_request'):invalid('goal_mode: 只能为continue或adopt_user_request；不能提供模型自写全局目标。')
    if type(body['expected_plan_version']) is not int or body['expected_plan_version']<0:invalid('expected_plan_version: 复制当前Plan版本整数；无Plan时为0。')
    minimum=0 if allow_empty_baseline and body['expected_scene'] is None else 1
    if type(body['expected_working_version']) is not int or body['expected_working_version']<minimum:invalid('expected_working_version: 复制当前Working版本正整数；获批空基线才可为0。')
    if not isinstance(body['update'],dict) or set(body['update'])-{'changes','order','focus_item_id'}:
        invalid('update: 只接受changes、order、focus_item_id；不是items/focus。changes为[{item_id: 工作项ID, set: 意图字段对象}]；不要传内部update_id/expected_version。')
    from .refinement_plan import RefinementError
    try:update=validate_update({**body['update'],'update_id':op_id,'expected_version':max(1,body['expected_plan_version'])})
    except RefinementError as error:
        raise RefinementError('update (changes[].item_id/set、order、focus_item_id): '+str(error),
                              error.code,error.status,current_version=error.current_version) from None
    for index,row in enumerate(update.get('changes',[])):
        if row.get('observations') or set(row.get('set',{}))-{'title','desired','basis','criteria','object_ids','status','issue_ids'}:
            invalid(f'update.changes[{index}].set/observations: 仅可写title/desired/basis/criteria/object_ids/status/issue_ids；不写method、执行或质量事实，observations须省略或为空。')
    return update

def apply(store, parent, decision, call, body, *, known_ids):
    """One receipt/transaction; repeat lookup precedes current-state CAS."""
    op_id=digest({'parent':parent['id'],'decision':decision['decision_id'],'call':call['id']})
    fingerprint=digest(body)
    with store.state._write() as c:
        prior=c.execute('SELECT request_digest,document FROM director_metadata WHERE metadata_id=?',(op_id,)).fetchone()
        if prior:
            if prior['request_digest']!=fingerprint: reject('元数据身份内容冲突。','director_metadata_conflict',409)
            return json.loads(prior['document'])
        row=c.execute('SELECT document FROM director_decisions WHERE decision_id=?',(decision['decision_id'],)).fetchone()
        claim=json.loads(row[0]) if row else None
        calls=(claim or {}).get('assistant',{}).get('tool_calls',[])
        if (not claim or claim['parent_id']!=parent['id'] or claim['state']!='returned'
            or not any(row['id']==call['id'] and row['function']['name']=='director_goal_plan'
                       and digest(json.loads(row['function']['arguments']))==fingerprint for row in calls)):
            reject('元数据缺少原已确认native决策/工具身份。','director_metadata_unbound',409)
        session=json.loads(c.execute('SELECT document FROM director_sessions WHERE parent_id=?',(parent['id'],)).fetchone()[0])
        accepted=session['run']
        task=json.loads(c.execute('SELECT document FROM director_tasks WHERE slot=1').fetchone()[0])
        scope=session.get('request_scope') or {}
        continuation=session.get('creative_continuation') or {}
        creative=task.get('creative_authorization') or {}
        continued=(accepted.get('request_protocol') in {'natural_v1','natural_v2'} and scope.get('intent') in {'edit','continuous'}
            and continuation.get('mode')=='successor' and scope.get('creative_continuation')==continuation
            and continuation.get('task_id')==task['task_id']
            and continuation.get('authorization_id')==creative.get('authorization_id')
            and continuation.get('accepted_request_digest')==digest({'parent_id':parent['id'],'request_fingerprint':accepted['request_fingerprint']}))
        update=validate(body,op_id,allow_empty_baseline=bool(accepted.get('creation') and creative or continued))
        interpreted=((accepted.get('request_protocol') in {'natural_v1','natural_v2'} or session.get('natural_recovery'))
                     and scope.get('intent') in {'edit','continuous'} and scope.get('state') in {'classified','active'})
        finite=interpreted and (scope['intent']=='edit' or accepted.get('execution_policy_version')==2 or session.get('natural_execution',{}).get('version')==2)
        if store.pause_blocks(task,accepted,scope,c=c) or not (task['policy']['autonomous'] or finite):
            reject('新目标/Plan不能解除持续任务暂停或增加自主权限。','director_autonomy_paused',409)
        active=any(json.loads(row[0])['state'] in {'reserved','prepared','started','waiting'}
                   for row in c.execute('SELECT document FROM director_delegations'))
        if active or c.execute("SELECT 1 FROM local_rounds WHERE status IN ('active','paused') LIMIT 1").fetchone():
            reject('仍有施工/草稿写入claim，不能改全局目标。','director_metadata_busy',409)
        project=json.loads(store.state._read_row(c)['manifest'])
        goal_row=c.execute('SELECT snapshot FROM refinement_goals ORDER BY version DESC LIMIT 1').fetchone()
        goal=json.loads(goal_row[0]) if goal_row else None
        plan_row=c.execute('SELECT document FROM refinement_plans WHERE singleton=1').fetchone()
        plan=json.loads(plan_row[0]) if plan_row else None
        if (reference(goal)!=body['expected_goal_ref'] or (plan['version'] if plan else 0)!=body['expected_plan_version']
            or selection_identity(project.get('current_scene'))!=body['expected_scene']
            or project['workspace']['working_version']!=body['expected_working_version']
            or project['revision']!=parent['expected_project_revision']):
            reject('场景/Goal/Plan或持续任务引用已变化。','director_metadata_stale',409)
        binding_row=c.execute('SELECT binding FROM refinement_bindings WHERE run_id=?',(parent['id'],)).fetchone()
        binding=json.loads(binding_row[0]) if binding_row else None
        if binding and binding['goal']['id']!=goal['id']:
            reject('旧父绑定已失效，不能静默重定向。','director_metadata_stale',409)
        from .director_goal_continuity import continue_goal
        continue_goal(store,c,parent,task,goal,plan,project,error='director_metadata_stale')
        previous=goal
        progress_before=semantic_state(goal,plan,project,claim,session)
        if goal is None and body['goal_mode']=='continue':
            reject('没有可延续的既有Goal；新施工需明确采纳用户请求。','director_goal_missing',409)
        adopted=body['goal_mode']=='adopt_user_request'
        if adopted:
            if binding or c.execute('SELECT 1 FROM director_metadata WHERE parent_id=? AND adopted=1',(parent['id'],)).fetchone():
                reject('一个父请求只在首次施工边界采纳一次目标。','director_goal_already_bound',409)
            if not interpreted and discussion_request(accepted['prompt']):
                reject('讨论/澄清文字不能成为新施工目标。','director_goal_discussion',409)
            proposal={**accepted,'operation':'conversation' if interpreted else 'edit','explicit_constraints':copy.deepcopy(accepted.get('constraints',{})),
                      'evaluation':accepted.get('evaluation',{'mode':'intent'})}
            content,_=goal_content(proposal,project,goal)
            content_hash=digest(content)
            if goal is None or content_hash!=goal['content_digest']:
                from .project_state_store import _now
                goal={'id':uuid.uuid4().hex,'version':goal['version']+1 if goal else 1,'project_id':store.state.project_id,
                      'content':content,'content_digest':content_hash,'source':{'kind':'director_user_request',
                       'run_id':parent['id'],'metadata_id':op_id},'created_at':_now()}
                c.execute('INSERT INTO refinement_goals VALUES (?,?,?)',(goal['id'],goal['version'],encoded(goal)))
        for change in update.get('changes',[]):
            fields=change.get('set',{})
            if set(fields.get('object_ids',[]))-set(known_ids):
                reject('Plan引用不是实际Working物体。','refinement_reference_invalid',409)
            if set(fields.get('issue_ids',[]))-{row['id'] for row in project['workspace'].get('issues',[])}:
                reject('Plan Issue引用不是实际问题。','refinement_reference_invalid',409)
        goal_changed=previous is None or goal['id']!=previous['id']
        if plan is None:
            from .project_state_store import _now
            plan={'schema_version':1,'id':uuid.uuid4().hex,'project_id':store.state.project_id,'version':0,
                  'goal_id':goal['id'],'focus_item_id':None,'order':[],'items':[],'created_at':_now()}
        proposed=copy.deepcopy(plan); proposed['goal_id']=goal['id']
        proposed=apply_update(proposed,update,{})
        for row in proposed['items']:
            if row['id'] in {change['item_id'] for change in update.get('changes',[])}: row['author']='director'
        comparable=lambda value:{k:v for k,v in value.items() if k!='version'}
        plan_changed=comparable(proposed)!=comparable(plan)
        if plan_changed:
            c.execute('INSERT OR REPLACE INTO refinement_plans VALUES (1,?)',(encoded(proposed),))
            c.execute('INSERT INTO refinement_plan_versions VALUES (?,?)',(proposed['version'],encoded(proposed)))
            plan=proposed
        if binding is None:
            binding={'run_id':parent['id'],'project_id':store.state.project_id,'goal':goal,'plan_id':plan['id'],
                     'bound_plan_version':plan['version'],'scene':selection_identity(project.get('current_scene')),
                     'working_version':project['workspace']['working_version'],'request':accepted['prompt'],'discussion':False,
                     'director_metadata_id':op_id,'director_goal_mode':body['goal_mode']}
            c.execute('INSERT INTO refinement_bindings VALUES (?,?)',(parent['id'],encoded(binding)))
        if not task.get('progress_guard_ref'): task['progress_guard_ref']=task.get('goal_ref') or reference(previous) or reference(goal)
        task['goal_ref']=reference(goal)
        if goal_changed:
            task['goal_changes']=(task.get('goal_changes',[])+[{'from':reference(previous),'to':reference(goal),
                'parent_id':parent['id'],'metadata_id':op_id}])[-4:]
        task['revision']=task.get('revision',1)+1
        c.execute('UPDATE director_tasks SET document=? WHERE slot=1',(encoded(task),))
        result={'metadata_id':op_id,'goal_ref':reference(goal),'goal_mode':body['goal_mode'],
                'goal_changed':goal_changed,'plan_changed':plan_changed,'plan_id':plan['id'],'plan_version':plan['version'],
                'formal_scene_save':False,'working_version':project['workspace']['working_version']}
        result['semantic_progress']={'before':progress_before,'after':semantic_state(goal,plan,project,claim,session),
            'attempted':{'mode':body['goal_mode'],'item_ids':[row['item_id'] for row in update.get('changes',[])],
                         'order_requested':'order' in update,'focus_requested':'focus_item_id' in update}}
        c.execute('INSERT INTO director_metadata VALUES (?,?,?,?,?,?,?)',
                  (op_id,parent['id'],decision['decision_id'],call['id'],fingerprint,int(adopted),encoded(result)))
        return result

def plan_authorization(plan, item_ids):
    indexed={row['id']:row for row in plan['items']}
    if set(item_ids)-set(indexed): reject('Director需先初始化全部授权工作项。','director_plan_unbound',409)
    return {'plan_id':plan['id'],'plan_version':plan['version'],'goal_id':plan['goal_id'],
            'items':{key:{field:copy.deepcopy(indexed[key][field]) for field in PROTECTED} for key in item_ids}}

def check_builder(brief, body):
    from .refinement_plan import PlanRejected
    authorization=brief.get('plan_authorization')
    if not authorization: return  # Existing stored legacy bindings are not backfilled.
    for index,change in enumerate(body.get('changes',[])):
        frozen=authorization['items'].get(change['item_id'])
        if frozen is None:
            raise PlanRejected('局部子步骤请使用已有方法/next_step，不新增全局项。','director_scope_denied',409,
                               fields=[f'changes[{index}].item_id'])
        for field,value in change.get('set',{}).items():
            if field in PROTECTED and value!=frozen[field]:
                raise PlanRejected('Builder不能改变Director冻结的意图/验收效果/对象范围。','director_scope_denied',409,
                                   fields=[f'changes[{index}].set.{field}'])
