"""Run-bound asset tools and complete, bounded native pause exchanges."""
import copy
import hashlib
import json
import uuid

from .asset_provider import AssetInputRejected, AssetJobError, digest


def tool(name, description, properties, required):
    return {'type':'function', 'function':{'name':name, 'description':description,
        'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}


TOOLS = [
    tool('request_asset', 'Local Hunyuan: reference_id only, never prompt. Cloud: prompt only, never reference_id. permission_request_id continues a frozen local purpose; reason only explains this action. A new intent requires request_resources.',
         {'prompt':{'type':'string','maxLength':1024},'reference_id':{'type':'string'},
          'permission_request_id':{'type':'string','description':'Existing SQL local permission application ID; does not change its frozen purpose.'},
          'reason':{'type':'string'}}, ['reason']),
    tool('read_asset_request', 'Read current local asset facts without remote calls.', {}, []),
    tool('import_asset', 'Import the ready shape into an isolated draft with explicit whole-object placement. Not a formal save.',
         {'request_id':{'type':'string'},'pose':{'type':'object','properties':{
             'position':{'type':'array','items':{'type':'number'},'minItems':3,'maxItems':3},
             'rotation':{'type':'array','items':{'type':'number'},'minItems':3,'maxItems':3},
             'scale':{'type':'array','items':{'type':'number'},'minItems':3,'maxItems':3}},
             'additionalProperties':False}}, ['request_id','pose']),
    tool('transform_asset', 'Move/rotate/scale one registered asset as an isolated local-round draft, then review. Not a save.',
         {'instance_id':{'type':'string'},'changes':{'type':'object'},'reason':{'type':'string'}},
         ['instance_id','changes','reason'])]

TOOLS[0]['function']['parameters']['oneOf'] = [
    {'required':['reference_id'], 'not':{'required':['prompt']}},
    {'required':['prompt'], 'not':{'anyOf':[{'required':['reference_id']}, {'required':['permission_request_id']}]}}]

INSTRUCTIONS = """One frozen asset intent is available in bootstrap and local rounds. For cloud request_asset use prompt; human fee approval is separate. For local Hunyuan use reference_id from the trusted asset references, never prompt, a local path or invented image. Continue an existing local purpose using permission_request_id from local_asset_capability.permissions; the host reads its original frozen purpose. reason describes this action and never changes that purpose. Without an ID the host binds only an unambiguous application. If several exist, choose the ID for the intended purpose; to propose a genuinely new intent or reapply after denial, explicitly use request_resources(local_asset_permission) and await a new human decision. Task total quota AND the current bootstrap/local-round permission are enforced separately by SQL. request_asset first requests a missing quota or stage permission through request_resources and pauses this SAME Run; approval never resets usage, denial returns to you to choose a procedural alternative. Capability availability is not execution authority. The full tool package is paired before waiting. A confirmed local failure is shown in asset_request.failure_feedback: the original call has finished, the generation was consumed, and its Job must not replay. Choose a procedural alternative, or request_resources with one task local_generations for a new attempt of this same asset; a new human approval binds the old request/Job/reference/current stage. After approval request_asset creates a new Job, without changing the original failure. Refusal returns here to choose an alternative. After ready, read_asset_request and decide whether to import with explicit pose; a bootstrap must first create_scene if no candidate exists. import_asset and transform_asset create isolated drafts and observation evidence; review, self-check and use the ordinary formal commit and independent Judge. Transforming an existing asset does not require generation permission. No request/read/transform is a save. Unknown writes cannot be retried or assigned a new identity."""


def exchange(history):
    from .builder_context import latest_exchange, scrub
    from .providers import _agent_history
    rows = latest_exchange(history)
    if not rows or not rows[0].get('tool_calls') or len(rows) > 33:
        raise AssetJobError('asset_tool_exchange_unsettled')
    _agent_history(rows)
    # Snapshot compact tool views only; never restore images or private reasoning.
    # Keep native function arguments byte-for-byte for repeated-ID conflict checks.
    # Strip private reasoning and sanitize only the business replies.
    rows[0].pop('reasoning_content',None)
    if 'content' in rows[0]: rows[0]['content'] = scrub(rows[0]['content'])
    for row in rows[1:]: row['content'] = scrub(row['content'])
    if len(json.dumps(rows,ensure_ascii=False).encode('utf-8')) > 65536:
        raise AssetJobError('asset_tool_exchange_size_limit')
    _agent_history(rows)
    return rows


def restore(agent, doc):
    rows = doc['checkpoint'].get('native_exchange')
    if not rows or digest(rows) != doc['checkpoint'].get('native_exchange_digest'):
        raise AssetJobError('asset_tool_exchange_unsettled')
    from .providers import _agent_history
    _agent_history(rows)
    agent.history.extend(copy.deepcopy(rows))
    results = {r['tool_call_id']:json.loads(r['content']) for r in rows[1:]}
    for call in rows[0]['tool_calls']:
        function = call['function']
        fingerprint = hashlib.sha256((function['name']+function['arguments']).encode()).hexdigest()
        agent.tool_results[call['id']] = (fingerprint,results[call['id']])


def request(agent, arguments):
    cap = agent.manager._asset_host().store(agent.manager,agent.identifier).capability(agent.identifier)
    local = cap and cap["profile"]["provider_id"] == "hunyuan"
    guidance = ('Local Hunyuan requires trusted reference_id and reason; omit prompt.'
                if local else 'Cloud generation requires prompt and reason; omit reference_id and permission_request_id.')
    if not isinstance(arguments, dict):
        raise AssetInputRejected('invalid_asset_request', ['arguments'], guidance)
    extra = set(arguments)-{'prompt','reference_id','reason','permission_request_id'}
    if extra:
        raise AssetInputRejected('invalid_asset_request', sorted(extra), guidance)
    if ('prompt' in arguments) == ('reference_id' in arguments):
        raise AssetInputRejected('invalid_asset_request', ['prompt','reference_id'], guidance+' Supply exactly one input.')
    reason = arguments.get('reason')
    if not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 2000:
        raise AssetInputRejected('invalid_asset_request', ['reason'], guidance+' reason must be nonempty text of at most 2000 characters.')
    reason = reason.strip()
    request_id = arguments.get('permission_request_id')
    from .asset_provider import identifier
    if 'permission_request_id' in arguments:
        if not local:
            raise AssetInputRejected('invalid_asset_request', ['permission_request_id'], guidance)
        try: identifier(request_id)
        except AssetJobError:
            raise AssetInputRejected('invalid_asset_request', ['permission_request_id'], 'Use the existing SQL permission_request_id shown in context.') from None
    if local:
        try: reference_id = identifier(arguments.get('reference_id'))
        except AssetJobError:
            raise AssetInputRejected('invalid_asset_request', ['reference_id'], guidance) from None
        source = {"kind":"image","reference_id":reference_id}
    else:
        prompt = arguments.get("prompt")
        if not isinstance(prompt,str) or not 0 < len(prompt.strip()) <= 1024:
            raise AssetInputRejected('invalid_asset_prompt', ['prompt'], guidance+' prompt must contain 1–1024 characters.')
        source = {"kind":"text","prompt":prompt}
    rd = agent.manager.get_local_round(agent.identifier) if agent.local_round_mode else None
    if agent.state.get('pending_revision') or not agent.run.get('bootstrap') and (not rd or rd['status'] != 'active'):
        raise AssetJobError('asset_pause_not_safe')
    doc = agent.manager._asset_host().store(agent.manager,agent.identifier).request(agent.identifier)
    retry=None
    if doc:
        old_input = {"kind":"image","reference_id":doc.get("reference",{}).get("reference_id")} if local else doc['request']['input']
        bound = (doc.get('stage_permission') or {}).get('request_id')
        if (old_input != source or (not bound and doc.get('reason') != reason and not doc.get('failure_feedback'))
                or request_id is not None and request_id != bound):
            raise AssetJobError('asset_slot_conflict')
        if not local or not doc.get('failure_feedback'):
            return {'asset_request':agent.manager.get_asset_request(agent.identifier)}
        from .local_asset_failure import confirmed, approved_retry
        state=agent.manager._projects().state_store(agent.run['project_id'])
        if not confirmed(state,doc):raise AssetJobError('asset_local_failure_unconfirmed')
        retry=approved_retry(state,doc)
        if not retry:
            return agent.manager._resource_host().request_tool(agent,{
                'reason':reason+'（原生成已确认失败且次数已计入；申请同一资产新的本地尝试。）',
                'next_step':'批准后在原候选继续，以新作业生成一次；拒绝后选择程序化方案。',
                'resources':[{'resource':'local_generations','scope':'task','delta':1}]})
        reason=doc['reason']
    permission = None
    if local:
        from .local_asset_permission import bind, authorized
        permission = copy.deepcopy(doc['stage_permission']) if retry else bind(agent.manager, agent.manager.get(agent.identifier), source['reference_id'], reason, request_id)
    if local and agent.run.get('agent_resource_base'):
        from .asset_local_authorization import LocalAssetAuthority
        auth = LocalAssetAuthority(agent.manager._projects().state_store(agent.run['project_id'])).read(**cap['local_authorization'])
        if auth['used_generations'] >= auth['effective_max_generations']:
            # No Asset request/job/claim has been created. Pair this original
            # tool batch and ask the human for the original task's local pool.
            return agent.manager._resource_host().request_tool(agent, {
                'reason': reason + '（原task本地生成额度不足，尚未提交生成。）',
                'next_step': '批准后由同一Builder重新决定原本地资产请求；不授权云端费用。',
                'resources': [{'resource': 'local_generations', 'scope': 'task', 'delta': 1}]})
    if local:
        if not authorized(agent.manager, agent.manager.get(agent.identifier), permission, require=False):
            return agent.manager._resource_host().request_tool(agent, {'reason':reason,
                'next_step':'当前轮本地许可决定后，同一Builder自主决定生成或替代方案；不批准云端费用。',
                'resources':[], 'local_asset_permission':{'reference_id':source['reference_id'],'purpose':reason,'generation_count':1}})
    # The executor persists this only after all siblings have real not_executed replies.
    agent.asset_pause = {'input':source,'parameters':{},'reason':reason,**({'stage_permission':permission} if local else {})}
    from .local_asset_failure import retry_identity
    return {'request_id':retry_identity(doc,retry) if retry else uuid.uuid5(uuid.UUID(agent.run['project_id']),f'{agent.identifier}:asset-slot:1').hex,
            'phase':'approval','awaiting_human_approval':not local,
            **({'permission_request_id':permission['request_id'], 'permission':permission, 'approved':True}
               if permission and permission.get('request_id') else {})}


def transform_asset(agent, arguments):
    """Typed asset_transform draft: original step identity and immutable receipt."""
    agent._args(arguments,["instance_id","changes","reason"])
    from .asset_contract import AssetError, instance_id, transform
    from .visual_agent import ToolInputError
    try:
        identifier = instance_id(arguments["instance_id"])
        changes = transform(arguments["changes"],partial=True)
    except AssetError as error:
        raise ToolInputError(str(error)) from None
    reason = agent._text(arguments["reason"])
    if not (agent.local_round_mode or agent.run.get('bootstrap')) or agent.state.get("pending_revision"):
        raise ToolInputError("Asset transform requires an active local round without a pending draft.")
    base = agent._revision(agent.state["selected_revision"])
    agent._verify_revision(base)
    if base["id"] not in agent.seen_revisions:
        raise ToolInputError("Observe the current base before transforming an asset.")
    plan = json.loads((agent._workspace(base)/"plan.json").read_text(encoding="utf-8"))
    prior = next((row for row in plan.get("assets",{}).get("instances",[]) if row["instance_id"]==identifier),None)
    if not prior:
        raise ToolInputError("Asset instance is not registered in this candidate.")
    if all(prior["transform"][key] == value for key,value in changes.items()):
        raise ToolInputError("Asset transform has no geometric change.")
    agent._check_budget(edit=True,observations=2)
    if agent.manager.get(agent.identifier)["usage"]["brain_calls"] >= agent.limits["max_brain_calls"]:
        raise ToolInputError("No remaining Builder call to review the transform.")
    project = agent.manager._projects().state_store(agent.run["project_id"])
    if agent.local_round_mode: number = project.local_edit_attempt_count(agent.identifier)+1
    else:
        from .agent_resources import project as resources
        number=next(r['used'] for r in resources(agent.manager.get(agent.identifier),project,agent_state=agent.state)['resources'] if r['resource']=='draft_attempts' and r['scope']=='run')+1
    revision = {"id":f"r{number:03d}","parent_id":base["id"],"round":number,
                "status":"pending","edit_reason":reason,"artifact_ids":[],
                "operations":[{"op":"asset_transform","instance_id":identifier,"changes":changes}]}
    request = {"reason":reason,"operations":copy.deepcopy(revision["operations"])}
    # Core owns this additive keyword/SQL kind registration. Default draft_edit
    # behavior is preserved; never pretend this operation is an import.
    if agent.local_round_mode:
        step = agent.manager.start_local_step(agent.identifier,revision["id"],base["id"],request,kind="asset_transform")
    else:
        if agent.run.get('director_delegation'):
            from .director_creation import check_operations
            agent.manager._director_host().child_admission(agent.manager.get(agent.identifier))
            check_operations(agent.manager._director_host(),agent.run,request['operations'],base_revision=base['id'])
        operation=agent.manager._scene_operation(agent.identifier,revision['id'],'initial')
        attempt_id=hashlib.sha256((operation['operation_id']+':attempt:0').encode()).hexdigest()
        project.begin_attempt(operation['operation_id'],attempt_id)
        step={**operation,'attempt_id':attempt_id}
    revision["step_id"] = step["attempt_id"]
    agent.state["used"]["edit_rounds"] = number
    target = agent._workspace(revision)
    try:
        applied = agent.adapter.transform_asset(agent._workspace(base),target,identifier,changes)
        agent._verify_revision(base)
        row = applied.get("asset",{})
        if row.get("source") != prior["source"] or row.get("root_id") != prior["root_id"]:
            raise AssetJobError("asset_transform_identity_conflict")
        result = agent._publish(revision)
        from .asset_artifacts import write_json_once
        write_json_once(target/"asset-transform-receipt.json",
            {"schema_version":1,"operation_id":step["operation_id"],"attempt_id":step["attempt_id"],
             "run_id":agent.identifier,"base_revision":base["id"],"revision_id":revision["id"],
             "request":request,"asset":row,"artifacts":agent.manager._fixed_scene_artifacts(agent.manager.get(agent.identifier),revision["id"])})
        agent.state["revisions"].append(revision)
        agent.state["pending_revision"] = revision["id"]
        agent.state["actions"].append({"revision_id":revision["id"],"reason":reason,
            "operations":copy.deepcopy(revision["operations"]),"status":"pending","type":"asset_transform"})
        project.record_attempt_result(step["attempt_id"],outcome="succeeded")
        if agent.local_round_mode:
            agent.manager.finish_local_step(agent.identifier,step["attempt_id"],status="pending",agent_state=agent.state)
        return {**result,"asset":row,"draft_only":True,"working_unchanged":True}
    except Exception as error:
        outcome = getattr(error,"outcome",None)
        rejected = outcome in {"rejected","not_scheduled"}
        # A finish ACK may be lost after the successful attempt/receipt was
        # committed. Preserve that fact instead of writing a conflicting result.
        with project._connection() as connection:
            attempt = connection.execute('SELECT status FROM edit_attempts WHERE attempt_id=?',
                                         (step['attempt_id'],)).fetchone()
        if not attempt or attempt[0] != 'succeeded':
            project.record_attempt_result(step["attempt_id"],outcome="failed" if rejected else "unknown",
                failure_category="confirmed_rejected" if rejected else "write_unknown",
                confirmed_no_commit=rejected,detail="asset transform "+type(error).__name__)
            if agent.local_round_mode:
                agent.manager.finish_local_step(agent.identifier,step["attempt_id"],status="failed" if rejected else "unknown",detail=error)
        if rejected:
            raise
        agent.state["uncertain_write"] = {"run_id":agent.identifier,"revision_id":revision["id"],
                                         "workspace":str(target),"operations":request["operations"]}
        raise agent.manager.blender.BlenderError("Asset transform outcome unknown; do not replay.",
                                                outcome="unknown",code="write_result_unknown") from None


def pause(agent):
    rows = exchange(agent.history)
    if agent.run.get('bootstrap'):
        from .bootstrap_checkpoint import capture
        from .builder_continuation import seal
        from .agent_resource_store import ResourceStore
        cp = capture(agent, '本地资产生成等待检查点', allow_empty=True)
        cp['native'] = seal(agent, cp)
        ResourceStore(agent.manager._projects().state_store(agent.run['project_id'])).save_checkpoint(cp)
        agent.asset_pause['continuation_checkpoint'] = cp['id']
    agent.manager.request_asset(agent.identifier,agent.asset_pause,agent_state=agent.state,
        exchange={'settled':True,'native_exchange':rows,'mode':'builder_v1'})
    agent.asset_pause = None


def read(agent, arguments):
    agent._args(arguments,[])
    return {'asset_request':agent.manager.get_asset_request(agent.identifier)}


def import_asset(agent, arguments):
    agent._args(arguments,['request_id','pose'])
    host = agent.manager._asset_host()
    doc = host.store(agent.manager,agent.identifier).request(agent.identifier)
    if not doc or doc['id'] != arguments['request_id']:
        raise AssetJobError('asset_request_binding_conflict')
    from .visual_agent import ToolInputError
    if not isinstance(arguments['pose'],dict):
        raise ToolInputError('Asset pose requires an explicit object.')
    from .asset_contract import AssetError, transform
    try:
        pose = transform(arguments['pose'])
    except AssetError as error:
        raise ToolInputError(str(error)) from None
    if doc['closed']:
        raise AssetJobError('asset_wait_ended')
    if not agent.state.get('selected_revision'):
        raise ToolInputError('资产已可用；先create_scene建立当前初建候选，再决定导入。')
    if doc['import_intent'] and doc['pose'] != pose:
        raise AssetJobError('asset_import_identity_conflict')
    agent._check_budget(edit=not bool(doc['import_receipt']), observations=0 if doc['import_receipt'] else 2)
    try:
        host.import_candidate(agent.manager,agent.identifier,agent_state=agent.state,pose=pose)
    except Exception as error:
        host.store(agent.manager,agent.identifier).fail_import(agent.identifier,
            confirmed_rejected=getattr(error,'outcome',None) == 'rejected')
        host.sync(agent.manager,agent.identifier)
        raise
    current = host.store(agent.manager,agent.identifier).request(agent.identifier)
    if current['closed'] or current.get('import_history'):
        raise AssetJobError('asset_wait_ended')
    if not doc.get('import_receipt'):
        agent.state = copy.deepcopy(current['import_receipt']['agent_state'])
    revision = agent._revision(current['import_intent']['revision_id'])
    return {'revision':copy.deepcopy(revision), 'asset':copy.deepcopy(current['import_receipt']['asset']),
            'inspection':agent._inspect({'revision_id':revision['id']}),
            'observations':[copy.deepcopy(o) for o in agent.state['observations'] if o['revision_id'] == revision['id']],
            'draft_only':True,'working_unchanged':True}
