"""Bounded public projection of existing Director records; never executes work."""
import base64
import json
import re
from .director_contract import digest,reject

DEFAULT_LIMIT=40
MAX_LIMIT=100
TOOLS={'director_request_scope':'确认本次请求范围','director_observe':'观察当前工作版',
       'director_goal_plan':'更新目标与工作计划','delegate_task':'委派局部施工',
       'director_reply':'记录正式回复','director_clarify':'记录澄清问题'}
STAGES={'agent_planning':('builder','正在准备施工步骤'),'agent_tool':('builder','正在执行施工工具'),
    'agent_review':('builder','正在检查候选'),'agent_committing':('builder','正在提交工作版'),
    'completed':('builder','子执行已结束'),'cancelled':('builder','子执行已停止'),
    'failed':('builder','子执行失败'),'awaiting_resume':('builder','子执行等待核实'),
    'judging':('judge','独立评审正在处理真实保存结果')}

def safe_text(value):
    if not isinstance(value,str):return ''
    value=value[:4000]
    value=re.sub(r'(?i)\b(?:sk-|sk_|bearer\s+)[A-Za-z0-9_\-.]+','[已隐藏凭据]',value)
    value=re.sub(r'(?i)(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[=:]\s*[^\s,;]+','[已隐藏凭据]',value)
    value=re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s<>"\n]+','[已隐藏路径]',value)
    value=re.sub(r'data:[^\s]+','[已隐藏内联数据]',value)
    value=value.strip()
    return value if len(value)<=2000 else value[:1980]+'…（公开文字已截断）'

def cursor(project,parent,order):
    return base64.urlsafe_b64encode(json.dumps([1,project,parent,order],separators=(',',':')).encode()).decode().rstrip('=')

def boundary(project,parent,value):
    try:
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}',value):raise ValueError()
        version,pid,rid,order=json.loads(base64.urlsafe_b64decode(value+'='*((-len(value))%4)))
        if (version!=1 or pid!=project or rid!=parent or not isinstance(order,list) or len(order)!=3
            or any(type(n) is not int or not 0<=n<2**53 for n in order)):raise ValueError()
        return order
    except (ValueError,TypeError,UnicodeError):reject('过程游标无效或不属于本项目/请求。','director_timeline_cursor_invalid')

def entry(parent,identity,order,kind,role,status,text,**extra):
    return {'id':digest({'parent':parent,'entry':identity}),'order':order,'kind':kind,'role':role,
            'status':status,'text':text,**extra}

def decision_entries(parent,decision,bindings,runs,*,final_text=None):
    index=decision['call_index'];assistant=decision.get('assistant') or {};calls=assistant.get('tool_calls') or []
    if type(index) is not int or not 0<=index<2**53:return []
    rows=[];identity=decision['decision_id'];batch=decision.get('batch') or {};outcomes=batch.get('outcomes',[])
    # Pure text and final reply belong to Turn. Only intermediate public content
    # accompanying work tools is an independent commentary event.
    text=safe_text(assistant.get('content'))
    final=decision.get('decision') or next((r['decision'] for r in outcomes if r.get('decision')),None)
    duplicates_final=bool(final and final_text and assistant.get('content')==final_text==final.get('summary'))
    if text and calls and not duplicates_final:
        rows.append(entry(parent,[identity,'commentary'],[index,0,0],'commentary','director','completed',text))
    for position,call in enumerate(calls):
        name=call.get('function',{}).get('name')
        if name not in TOOLS:continue
        outcome=outcomes[position] if position<len(outcomes) else None
        if outcome is None and batch.get('started')!=position:
            # Legacy applied single calls have a proven exchange/receipt. A
            # returned unexecuted call is not a running operation.
            if decision.get('state')!='applied' or batch:continue
            outcome={'result':decision.get('decision') or {},'tool':{}}
        status='running';label='正在'+TOOLS[name]
        if outcome is not None:
            try:public=json.loads(outcome.get('tool',{}).get('content','{}'))
            except (ValueError,TypeError):public={}
            if public.get('ok') is False:
                code=(public.get('error') or {}).get('code') if isinstance(public.get('error'),dict) else ''
                status='unknown' if 'unknown' in (code or '') else 'blocked' if code in {
                    'requires_fresh_reasoning','child_result_pending','director_scope_denied','project_writer_active'} else 'failed'
                label={'unknown':'工具结果待核实：','blocked':'工具未执行：','failed':'工具未成功：'}[status]+TOOLS[name]
            else:status='completed';label='已'+TOOLS[name]
        elif decision.get('state')=='unknown' or decision.get('_parent_status') in {'failed','interrupted','cancelled','awaiting_resume'}:
            status='unknown';label='工具结果待核实：'+TOOLS[name]
        rows.append(entry(parent,[identity,call.get('id')],[index,1,position],'tool','director',status,label,tool_name=name))
        child_id=(outcome or {}).get('child_id') or (decision.get('child_id') if name=='delegate_task' else None)
        binding=bindings.get(child_id)
        if child_id and binding and binding.get('parent_id')==parent:
            child=runs.get(child_id) or binding.get('child_snapshot') or {}
            for event in child.get('events',[]):
                seq=event.get('seq');stage=event.get('stage','')
                if type(seq) is not int or not 0<=seq<2**53:continue
                if stage in STAGES:role,summary=STAGES[stage]
                else:continue
                # A phase records activity, not quality success. The last
                # nonterminal event stays running until the child moves on.
                current=(stage==child.get('stage') and seq==max((e.get('seq',0) for e in child.get('events',[])
                    if e.get('stage')==stage),default=0))
                state=('unknown' if current and (child.get('error') or {}).get('outcome_unknown')
                    else 'failed' if stage=='failed' else 'blocked' if stage in {'awaiting_resume','cancelled'}
                    else 'running' if current and child.get('status') in {'running','waiting_child'} else 'completed')
                if state=='completed' and stage not in {'completed','cancelled','failed'}:
                    summary='阶段已结束：'+('独立评审处理' if role=='judge' else '施工处理')+'；结论以回执为准'
                rows.append(entry(parent,[child_id,seq],[index,2,seq],'phase',role,state,summary,child_run_id=child_id))
            result=binding.get('result')
            if result:
                saved=result.get('saved_edit_rounds',0)
                rows.append(entry(parent,[child_id,'receipt'],[index,3,0],'phase','builder','completed',
                    '已确认正式保存'+str(saved)+'次；子执行状态与回执已保留',child_run_id=child_id))
                for n,review in enumerate(result.get('evaluations',[])):
                    applied=review.get('application_status')=='applied';verdict=review.get('verdict')
                    known=applied and review.get('status')=='completed' and not review.get('unknown',False)
                    state='unknown' if review.get('unknown') else 'failed' if review.get('status')=='failed' else 'completed' if known else 'blocked'
                    rows.append(entry(parent,[child_id,'judge',review.get('evaluation_id',n)],[index,3,n+1],
                        'phase','judge',state,
                        '独立评审回执：'+('已应用' if known else '待核实')+('，通过' if verdict=='pass' and known else ''),child_run_id=child_id))
    return sorted(rows,key=lambda row:row['order'])

def page(store,parent,runs,*,before=None,limit=DEFAULT_LIMIT):
    if type(limit) is not int or not 1<=limit<=MAX_LIMIT:reject('过程页数量必须为1–100。','director_timeline_limit_invalid')
    stop=boundary(store.state.project_id,parent,before) if before is not None else None
    rows=[]
    with store.state._connection() as c:
        if not c.execute('SELECT 1 FROM director_parents WHERE parent_id=?',(parent,)).fetchone():
            reject('过程只属于原Director父请求。','director_unbound',409)
        bindings={r['child_id']:r for r in (json.loads(x[0]) for x in c.execute('SELECT document FROM director_delegations WHERE parent_id=?',(parent,)))}
        project=json.loads(store.state._read_row(c)['manifest'])
        final_text=next((turn.get('assistant') for turn in project.get('workspace',{}).get('turns',[])
                         if turn.get('id')==parent and turn.get('assistant')),None)
        query='SELECT document FROM director_decisions WHERE json_extract(document,\'$.parent_id\')=?'
        args=[parent]
        if stop is not None:query+=' AND json_extract(document,\'$.call_index\')<=?';args.append(stop[0])
        query+=' ORDER BY json_extract(document,\'$.call_index\') DESC,decision_id DESC'
        for record in c.execute(query,args):
            decision=json.loads(record[0]);decision['_parent_status']=runs.get(parent,{}).get('status')
            candidates=decision_entries(parent,decision,bindings,runs,final_text=final_text)
            rows.extend(e for e in reversed(candidates) if stop is None or e['order']<stop)
            if len(rows)>limit:break
    has_more=len(rows)>limit;selected=list(reversed(rows[:limit]))
    return {'schema_version':1,'parent_run_id':parent,'entries':selected,'has_more':has_more,
            'before_cursor':cursor(store.state.project_id,parent,selected[0]['order']) if has_more and selected else None}
