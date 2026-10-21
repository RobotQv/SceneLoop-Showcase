"""Bounded Director facts and actual image bytes; no raw construction transcript."""
import copy
import json
from .director_contract import SYSTEM, digest
from .scene_evidence import build_scene_evidence
from .builder_context import scene_index
from .observation_service import working, director_images

SCOPE_GUIDANCE='''
Permissions in request_scope, authorization and capability apply ONLY to this current message.
For natural requests, policy.autonomous describes persistent continuous permission. An admitted finite edit is authorized by request_scope and authorization.can_dispatch with the actual allowed_tools; autonomous=false does not revoke that one scoped task or require another approval. Unapproved continuous proposals, explicit read-only requests, busy state and actual pauses remain governed by their current tools and authorization.
For an admitted edit, carry the necessary observation, Plan preparation, delegation and consumption of the real child result through the same request before your final reply. Do not end with only a proposed next step while implying construction will happen later. An evidenced no-op, actual blocker, clarification need or failure can justify ending without edits; explain the facts and never invent success or force construction. A reply reporting an already queued request or pending protected approval reports that existing state only.
For progress questions, report progress.main/child, current status and recent_events. A max_children=0 or writes=false for this question does NOT mean the separately running main/Builder is unauthorized or stopped. Do not replace current progress with an old analysis or ask for fresh direction solely because this question is read-only.
Read-only means no scene construction; director_observe may still be allowed when capability.renders=true. The actual allowed_tools and current busy state govern observation availability.
cost_summary.unknown_count/unsettled_count describe monetary confidence/reservations, not a count of unknown transport outcomes. unpriced means an amount is not priced, not a failed or unknown request. Use actual pause/error/status evidence for unknown operations. Do not recite internal IDs or default unpriced accounting in a normal progress reply; structured identity fields remain authoritative.
'''

MANAGED_GUIDANCE='''
This is natural execution policy version 2. The accepted human request is the authority for this goal. Decide discussion, inspection, finite edits or multiple local tasks yourself; edit/continuous are internal plans and never require another scope approval. policy.autonomous is legacy metadata, not an ordinary execution switch. Honor explicit analysis-only/do-not-modify requests, preservation and stopping instructions. Clarify genuinely missing intent in natural language; never ask the user to select a permission mode.
Continue admitted work through observation, Plan, Builder and real result consumption before replying. Existing cumulative resources, local-stage permission, cloud charges, low-resource warnings and unknown results remain separate hard boundaries. Model words or stop_conditions never increase budgets. A final reply does not schedule later work. During an active writer, progress uses existing evidence; new edits queue and rebind at idle. Old memory is opinion, not authority or proof of execution.
If queue.interpretation_required is true, interpret this same original human request again against the now verified Working/candidate. The earlier empty-project interpretation was a queued intention, not an object grant; copy actual object IDs from current facts and keep the original goal and constraints.
'''

def focused(rows, key, focus_id, limit=16):
    if focus_id is None: return rows[:limit]
    return ([row for row in rows if row.get(key)==focus_id]+[row for row in rows if row.get(key)!=focus_id])[:limit]

def feedback(manager, project_id):
    """Reuse independent input/files/criteria applicability; no aggregate inference."""
    from .refinement_evaluation import builder_feedback
    refinement=manager.get_refinement(project_id)
    projected=refinement['evaluation']
    value=builder_feedback(projected)
    focus=(refinement['plan'] or {}).get('focus_item_id')
    focus_item=next((row for row in (refinement['plan'] or {}).get('items',[]) if row['id']==focus),{})
    focus_issues=set(focus_item.get('issue_ids',[]))
    current_ids={row['item_id'] for row in value['item_results']}
    omitted={}
    for key in ('item_results','unresolved_issues'):
        omitted[key]=max(0,len(value[key])-16)
        value[key]=focused(value[key],'item_id',focus)
        if key=='unresolved_issues' and focus_issues:
            all_issues=builder_feedback(projected)['unresolved_issues']
            value[key]=([row for row in all_issues if row['id'] in focus_issues]+[row for row in all_issues if row['id'] not in focus_issues])[:16]
    failed=[row for row in projected['evaluations'] if row['application_status']!='applied'][-1:]
    value['last_failure']=[{**{k:copy.deepcopy(row[k]) for k in ('evaluation_id','input_id','working_version','selection',
        'application_status','current','reasons')},**{k:copy.deepcopy(row['result'][k]) for k in
        ('verdict','format_error','message','diagnostics') if k in row['result']}} for row in failed]
    required=[]
    for item in (refinement['plan'] or {}).get('items',[]):
        if item['id'] in current_ids: continue
        reviews=[review for row in projected['evaluations'] for review in row['item_results'] if review['item_id']==item['id']]
        required.append({'item_id':item['id'],'reasons':reviews[-1]['reasons'] if reviews else ['no_explicit_review']})
    value['item_review_required']=focused(required,'item_id',focus)
    omitted['item_review_required']=max(0,len(required)-16)
    value['omitted']=omitted
    value['meaning']='Explicit applicable item/Issue reviews only; aggregate PASS does not imply missing item reviews.'
    return value

def selection_view(value):
    return {key:copy.deepcopy(value[key]) for key in ('selection','working_version','blend_sha256','plan_sha256',
        'inspection_sha256','images') if key in value}

def completion_feedback(value):
    if isinstance(value,dict):
        return {('current_at_completion' if key=='current' else key):completion_feedback(child) for key,child in value.items()}
    if isinstance(value,list): return [completion_feedback(child) for child in value]
    return value

def construction_facts(manager, run):
    """SQL-backed draft facts, separate from committed Working differences."""
    state=manager._projects().state_store(run['project_id']);document=state.local_round(run['id'])
    drafts=[]; unverified=[]
    for step in (document or {}).get('steps',[]):
        if step['status'] not in {'pending','selected','rejected','needs_user_review'}:continue
        operation=state.operation_state(step['operation_id'])
        attempt=next((a for a in (operation or {}).get('attempts',[]) if a['attempt_id']==step['attempt_id']),None)
        if (not operation or operation['identity'].get('run_id')!=run['id']
                or operation['identity'].get('round_id')!=document['id'] or not attempt or attempt['status']!='succeeded'):
            unverified.append(step['revision_id']);continue
        try:
            fixed=manager._fixed_scene_artifacts(run,step['revision_id'])
            if fixed!=step.get('artifacts'):raise ValueError('draft artifact identity changed')
            manager.scene_version(run['id'],step['revision_id'],allow_active=True)
        except Exception:
            unverified.append(step['revision_id']);continue
        drafts.append({'run_id':run['id'],'revision_id':step['revision_id'],'operation_id':step['operation_id'],
            'attempt_id':step['attempt_id'],'status':step['status'],'reviewed':step.get('assessment') is not None,
            'artifacts':[{k:copy.deepcopy(a.get(k)) for k in ('kind','view_id','sha256','size')} for a in fixed],
            'render_views':[a['view_id'] for a in fixed if a['kind']=='render'],
            'formally_saved':bool(document['status']=='committed' and step['revision_id']==document['selected_revision']
                and document.get('receipt_operation_id') and state.lookup_receipt(document['receipt_operation_id']).get('status')=='committed')})
    correction=run.get('builder_tool_correction') or {};batches=correction.get('batches') or []
    last=batches[-1] if batches and batches[-1].get('brain_call')==run['usage']['brain_calls'] else None
    last_fact={'execution':last['execution'],'scope':'last_returned_batch','brain_call':last['brain_call']} if last else None
    error=run.get('error') or {};diagnostic=error.get('diagnostics') or {};parsed=diagnostic.get('response_parse') or {}
    if (last_fact is None and error.get('outcome_unknown') is False and diagnostic.get('finish_reason')=='tool_calls'
            and parsed.get('source')=='provider_response' and parsed.get('layer')=='tool_arguments'
            and parsed.get('run_id')==run['id'] and parsed.get('brain_call')==run['usage']['brain_calls']):
        last_fact={'execution':'not_executed','scope':'last_returned_batch','brain_call':parsed['brain_call'],
                   'source':'provider_argument_validation'}
    return {'source':'sql_attempt_and_verified_artifacts','drafts':drafts,'unverified_drafts':unverified,
            'pending_revision':(document or {}).get('pending_revision'),
            'last_batch':last_fact,
            'meaning':'Draft execution and renders are not Working publication or Judge approval; last-batch rejection never erases prior execution.'}


def reply_text(summary, result, next_focus=''):
    facts=(result or {}).get('construction') or {}
    pending=[d for d in facts.get('drafts',[]) if not d['formally_saved']]
    if not pending or not result.get('error'):return summary
    last=pending[-1];review='待Builder自检' if not last['reviewed'] else '已自检，尚未正式保存'
    note=f"执行事实：已生成草稿 {last['revision_id']} 及{len(last['render_views'])}张核验截图，{review}；这些草稿尚未成为Working，不能视为Judge通过。"
    if (facts.get('last_batch') or {}).get('execution')=='not_executed':note+='最后失败的工具批次未执行，之前施工仍保留。'
    return note+('\n\n后续建议：'+next_focus if next_focus else '')


def request(manager, parent, task, *, result=None, exchange=None):
    from . import creative_continuation
    continuation=creative_continuation.capture(manager._director_host(),parent)
    view,images=working(manager,parent['project_id'])
    observations,additional=director_images(manager,parent,view)
    images+=additional
    if view['selection']:
        evidence=scene_index(build_scene_evidence({'objects':view['objects'],'assets':view.get('assets',{}),
            **({'surface':view['surface']} if 'surface' in view else {})},
            view['inspection'],source={'run_id':view['selection']['run_id'],'revision_id':view['selection']['revision_id'],
                                      'working_version':view['working_version'],'working_scene':True}))
        view={**selection_view(view),'project_revision':view['project_revision'],
              'objects':evidence['editable_specification']['objects'],'evidence':evidence}
    state=manager._projects().state_store(parent['project_id']).refinement_state()
    plan=state['plan']
    if plan:
        plan={**{key:copy.deepcopy(plan[key]) for key in ('id','version','goal_id','focus_item_id','order')},
            'items':[{key:copy.deepcopy(row[key]) for key in ('id','title','desired','criteria','object_ids','status')}
                     for row in focused(plan['items'],'id',plan['focus_item_id'])],
              'omitted_items':max(0,len(plan['items'])-16)}
    result=copy.deepcopy(result)
    if result:
        for change in result.get('net_changes',[]):
            for side in ('before','after'):
                change[side]={key:(value if key in {'id','name','kind','position','rotation','scale','dimensions','key_object'}
                    else {'content_digest':digest(value),'detail_omitted':True}) for key,value in change.get(side,{}).items()}
    facts={'task_id':task['task_id'],'policy':task['policy'],'pause':task['pause'],
        'request':parent['prompt'],'working':view,'goal':state['goal'],
        'plan':plan,'result':result,'feedback':feedback(manager,parent['project_id']),
        'recent_decisions':task['decisions'][-4:],'observations':observations,
        'cost_summary':manager._director_host().cost_summary(parent['project_id'],task['task_id']),
        'goal_changes':task.get('goal_changes',[])[-4:]}
    if continuation:
        facts['creative_continuation']=creative_continuation.public(continuation)
        facts['creative_continuation']['meaning']='Verified unpublished candidate, not Working. Use its actual object/asset IDs. A terminal source requires one successor from this exact candidate and residual budgets; never create_scene. A same_run source keeps its existing resource approval and original native continuation.'
    from .director_readonly import asset_facts
    facts['assets']=asset_facts(manager,parent['project_id'],task['task_id'])
    from .director_request_scope import view as scope_view,enabled,managed,tools as scope_tools
    host=manager._director_host()
    facts.update(scope_view(host,parent))
    natural=enabled(host,parent)
    readonly=host.is_readonly(parent)
    if readonly:
        from .director_readonly import attach
        attach(manager,parent,facts)
    elif natural:
        names=[row['function']['name'] for row in scope_tools(host,parent)]
        facts['capability']={'mode':'request_scoped','source':'accepted_request_scope','allowed_tools':names,
            'writes':bool({'delegate_task','director_goal_plan'}&set(names)),'renders':'director_observe' in names,
            'permission_scope':'current_request_only'}
    accepted=host.accepted_request(parent)
    if parent.get('resource_budget'):
        facts['resource_budget']=copy.deepcopy(parent['resource_budget'])
        resource_host=manager._resource_host()
        request_doc=resource_host.store(parent).request(run_id=parent['id'])
        if request_doc:facts['resource_request']=resource_host.store(parent).public(request_doc)
    from .director_creation import authority
    creative=authority(host,parent)
    if creative and not readonly:
        from .director_creation import public_authority
        facts['creative_authorization']={**public_authority(creative),
            'meaning':'Explicit human project creation and new-object scope. Initial Working requires review_initial pass; later changes use local rounds. Stop conditions and total_saved_limit remain binding.'}
    from .project_references import resolve
    references=accepted.get('reference_snapshots')
    if references is None and creative and not readonly and not task.get('pause'):
        references=creative['approved'].get('reference_snapshots')
    for ref in references or []:
        record,path=resolve(manager,parent['project_id'],{k:ref[k] for k in ('artifact_id','sha256','purpose')})
        images += [{'type':'text','text':'ADOPTED PROJECT REFERENCE '+json.dumps(record,ensure_ascii=False)},manager.providers.image_content(path)]
    frozen={'task_id':task['task_id'],'parent_id':parent['id'],'user_goal':parent['prompt'],
            'constraints':accepted.get('constraints',{}),'preserve':accepted.get('constraints',{}).get('preserve','')}
    instruction=('\nInterpret this accepted human request with director_request_scope first. Discuss/inspect are not editing permissions. '
        'Every observation/child result must reach new reasoning before dependent actions. Do not infer ongoing authority from a model tool.'+
        (MANAGED_GUIDANCE if managed(host,parent) else SCOPE_GUIDANCE) if natural else
        '\nThis SQL-frozen request is read-only. Only reply/clarify are allowed. Cite the as-of Working and progress; drafts are not Working. Never delegate, edit metadata, render, change policy or cancel/resume any execution.' if readonly else '')
    instruction+='\nReport result.construction separately from net_changes: executed unpublished drafts and renders exist even when Working did not advance. The last rejected batch says nothing about earlier successful construction. Never report a draft as saved or Judge-approved.'
    messages=[{'role':'system','content':SYSTEM+instruction},{'role':'user','content':'DIRECTOR FROZEN REQUEST\n'+
        json.dumps(frozen,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)}]
    if exchange: messages+=exchange
    messages.append({'role':'user','content':[{'type':'text','text':'DIRECTOR CURRENT FACTS\n'+
        json.dumps(facts,ensure_ascii=False)}]+images})
    return messages,facts
