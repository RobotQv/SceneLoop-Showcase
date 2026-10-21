"""Bounded visual tool loop. Images are real artifacts; revisions are immutable."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from .asset_provider import AssetJobError
from .tool_correction import UnexecutedToolRejected, rejection_receipt

from .project_runtime import file_sha256
from .context_builder import refinement_context
from . import builder_context
from .providers import _agent_history as _validate_history, brain_timeout
from .prompts import REFERENCE_SCOPE_POLICY, SCENE_GEOMETRY_CONTRACT, VISUAL_REVIEW_POLICY
from .refinement_plan import RefinementError
from .observation_contract import validate_observation_metadata, comparison_identity
from .refinement_round import RoundError, enabled as local_round_enabled
from .refinement_budget import budget_progress, edit_limit, resolve_snapshot
from .scene_evidence import artifact_refs, build_scene_evidence, known_object_ids, source_identity
from .geometry_inspection_contract import compact_json, evidence_id as geometry_evidence_id
from .geometry_inspection_contract import finalize_document, validate_builder_request


class ToolInputError(ValueError, UnexecutedToolRejected):
    def __init__(self, message, *, code='tool_validation_error', details=None):
        super().__init__(message)
        self.code, self.details = code, details


class ToolResultError(ValueError):
    """The adapter already ran; its rejection is not a pre-execution claim."""
    code = 'tool_validation_error'
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details


class LoopStopped(Exception):
    def __init__(self, reason, summary):
        self.reason, self.summary = reason, summary


def limits_for(rounds):
    if type(rounds) is not int or not 1 <= rounds <= 6:
        raise ValueError("自动迭代轮数需要是 1–6 的整数。")
    return {"max_rounds": rounds, "max_brain_calls": 3 * rounds + 3,
            "max_observations": 2 * (rounds + 1) + 4,
            "max_tool_calls": 2 * (3 * rounds + 3), "max_seconds": 900}  # Historical metadata.


def _function(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


_TEXT = {"type": "string"}
_EVIDENCE = {"type": "array", "items": _TEXT}
_COLOR={'type':'array','minItems':3,'maxItems':3,'items':{'type':'number','minimum':0,'maximum':1}}
_XYZ={'type':'array','minItems':3,'maxItems':3,'items':{'type':'number'}}
_SURFACE_TARGET={'oneOf':[
    {'type':'object','additionalProperties':False,'required':['object_id'],'properties':{
        'object_id':_TEXT,'part_id':_TEXT,'slot':{'type':'integer','minimum':0}}},
    {'type':'object','additionalProperties':False,'required':['instance_id'],'properties':{
        'instance_id':_TEXT,'node_id':_TEXT,'slot':{'type':'integer','minimum':0}}}]}
_SURFACE={'type':'object','additionalProperties':False,'required':['recipe','color'],'properties':{
    'recipe':{'type':'string','enum':['wood','stone','metal','cloth','ground','paint']},'color':_COLOR,
    'roughness':{'type':'number','minimum':0,'maximum':1},'scale_m':{'type':'number','minimum':.001,'maximum':20},
    'bump_m':{'type':'number','minimum':0,'maximum':.02},'direction':{'type':'string','enum':['X','Y','Z']},
    'variation':{'type':'number','minimum':0,'maximum':1}}}
_WORLD={'oneOf':[
    {'type':'object','additionalProperties':False,'required':['type','strength','sun_elevation','sun_rotation'],
     'properties':{'type':{'const':'sky'},'strength':{'type':'number','minimum':0,'maximum':5},
        'sun_elevation':{'type':'number','minimum':-5,'maximum':90},'sun_rotation':{'type':'number','minimum':-360,'maximum':360}}},
    {'type':'object','additionalProperties':False,'required':['type','strength','color'],
     'properties':{'type':{'const':'color'},'strength':{'type':'number','minimum':0,'maximum':5},'color':_COLOR}}]}
_LIGHTS=[{'type':'object','additionalProperties':False,'required':['id','type','energy','color','rotation','angle'],
    'properties':{'id':_TEXT,'type':{'const':'SUN'},'energy':{'type':'number','minimum':0,'maximum':10},
        'color':_COLOR,'rotation':_XYZ,'angle':{'type':'number','minimum':0,'maximum':30}}}]
for _light_type in ('AREA','POINT'):
    _LIGHTS.append({'type':'object','additionalProperties':False,'required':['id','type','energy','color','position','size'],
        'properties':{'id':_TEXT,'type':{'const':_light_type},'energy':{'type':'number','minimum':0,'maximum':10000},
            'color':_COLOR,'position':_XYZ,'size':{'type':'number','minimum':.01,'maximum':100},
            **({'target':_XYZ} if _light_type=='AREA' else {})}})
_LIGHTING={'type':'object','additionalProperties':False,'required':['world','lights'],'properties':{
    'world':_WORLD,'lights':{'type':'array','maxItems':8,'items':{'oneOf':_LIGHTS}}}}
_EDIT_OPERATION={'anyOf':[
    {'type':'object','required':['op'],'properties':{'op':{'enum':['add','remove','update','add_part','update_part','remove_part','cut_box']}}},
    {'type':'object','additionalProperties':False,'required':['op','target','surface'],
        'properties':{'op':{'const':'assign_surface'},'target':_SURFACE_TARGET,'surface':_SURFACE}},
    {'type':'object','additionalProperties':False,'required':['op','lighting'],
        'properties':{'op':{'const':'configure_lighting'},'lighting':_LIGHTING}}]}
TOOLS = [
    _function("read_refinement_plan", "Read the persistent SQL work plan or selected item_ids, including current evidence validity and separate receipt facts. No scene writes or model calls.",
              {"item_ids": {"type": "array", "items": _TEXT}}),
    _function("update_refinement_plan", "Incrementally update Builder planning only. Requires unique update_id and expected_version from refinement.plan_version. changes: [{item_id, set:{title,desired,basis,criteria:[text],object_ids:[id],method,next_step,self_check,status:planned|active|blocked|parked,issue_ids,operation_ids,evaluation_ids}, observations:[{id,text,evidence:[{run_id,revision_id,artifact_id}]}]}]. New items require desired,basis,criteria. Evidence must use actual current/candidate evidence_refs; unknown, cross-project or stale refs are refused. Never write done/saved/judge_passed. order must contain every item ID. Exact repeats return original update receipt; conflicts require re-read.",
              {"update_id": _TEXT, "expected_version": {"type": "integer", "minimum": 1},
               "changes": {"type": "array", "items": {"type": "object"}}, "focus_item_id": {"type": ["string", "null"]},
               "order": {"type": "array", "items": _TEXT}}, ["update_id", "expected_version"]),
    _function("begin_local_round", "Bind one local goal and Plan criteria to current Working before any draft edit. Only for local_round_v1.",
              {"goal": _TEXT, "criteria": _TEXT,
               "item_ids": {"type": "array", "items": _TEXT}}, ["goal", "criteria", "item_ids"]),
    _function("checkpoint_local_round", "Freeze the current confirmed draft and pause this Run for explicit same-Run resume. Does not save Working.",
              {"reason": _TEXT}, ["reason"]),
    _function("commit_local_round", "After multiple or one confirmed draft steps, compare final draft with the round start and publish Working exactly once. Requires whole-round better+pass evidence.",
              {"verdict": {"type": "string", "enum": ["better", "equivalent", "worse", "uncertain"]},
               "preservation": {"type": "string", "enum": ["pass", "violation", "unknown"]},
               "reason": _TEXT, "evidence": _EVIDENCE},
              ["verdict", "preservation", "reason", "evidence"]),
    _function("end_local_round", "Explicitly end an open local draft round without publishing Working; preserve history.",
              {"reason": _TEXT}, ["reason"]),
    _function("judge_working_scene", "Request independent Judge for the current formally saved Working. End or commit an open draft round first.",
              {"reason": _TEXT}, ["reason"]),
    _function("create_scene", "Create the initial isolated ScenePlan. Also inspect and capture the two fixed comparison views.",
              {"plan": {"type": "object"}}, ["plan"]),
    _function("inspect_scene", "Read scene evidence: measured values, declared labels and editable plan parameters are separate sections. Call with object_ids for the complete measured parts and editable plan parameters of those objects. Inspect target objects before editing.",
              {"revision_id": _TEXT, "object_ids": {"type": "array", "items": _TEXT, "maxItems": 32}}, ["revision_id"]),
    _function("inspect_geometry", "Read the actual frozen mesh of one object/part: real vertex/edge/polygon counts, optional full mesh-local arrays, matrix_world and boundary/wire/non-manifold/component statistics. Read-only; does not edit or consume an Edit Round. Requires part_id when the object has multiple parts.",
              {"revision_id": _TEXT, "object_id": _TEXT, "part_id": _TEXT,
               "representation": {"type": "string", "enum": ["original", "evaluated"]}},
              ["revision_id", "object_id"]),
    _function("observe_scene", "Capture an appearance (default) or structure diagnostic view. Structure uses neutral inspection material/lighting, not original material/light quality evidence. Pending edits capture BOTH sides with identical mode/profile.",
              {"revision_id": _TEXT, "view": {"type": "object", "properties": {
                  "preset": {"type": "string", "enum": ["main", "auxiliary"]},
                  "mode": {"type": "string", "enum": ["appearance", "structure"]},
                  "position": {"type": "array", "items": {"type": "number"}},
                  "target": {"type": "array", "items": {"type": "number"}}, "lens": {"type": "number"}, "roll_degrees": {"type": "number", "minimum": -360, "maximum": 360}}}},
              ["revision_id", "view"]),
    _function("apply_edit", "Fork the selected revision and edit the new candidate, preserving the before. Operations are bounded add/remove/update or part edits; compound parts also accept a controlled cut_box opening/recess. Capture both fixed comparison views. Review it before another edit.",
              {"base_revision": _TEXT, "reason": _TEXT, "operations": {"type": "array", "items": _EDIT_OPERATION}},
              ["base_revision", "reason", "operations"]),
    _function("review_candidate", "Compare pending candidate against selected before and the fixed reference, citing observation IDs actually seen. Better+preserved selects it; worse/equivalent retains before; uncertain stops for user review.",
              {"candidate_id": _TEXT, "verdict": {"type": "string", "enum": ["better", "equivalent", "worse", "uncertain"]},
               "preservation": {"type": "string", "enum": ["pass", "violation", "unknown"]},
               "reason": _TEXT, "evidence": _EVIDENCE},
              ["candidate_id", "verdict", "preservation", "reason", "evidence"]),
    _function("respond", "Answer or ask a clarifying question without modifying the scene. Use this for discussion, diagnosis, explanation, or genuinely ambiguous edit intent. Optional evidence must cite observations seen this turn.",
              {"kind": {"type": "string", "enum": ["answer", "clarify"]},
               "message": _TEXT, "evidence": _EVIDENCE},
              ["kind", "message", "evidence"]),
    _function("save_checkpoint", "Create a durable manual checkpoint of the current committed Working Scene when the user explicitly asks to save a version.",
              {"title": _TEXT}, ["title"]),
    _function("finish", "Stop with an evidence-based explanation. Goal reached is a model judgement, not human acceptance. Use round_limit only if edit rounds are exhausted and the stage goal remains unmet. Resolve pending comparisons first, unless requesting user review.",
              {"reason": {"type": "string", "enum": ["goal_reached", "no_progress", "needs_user_review", "capability_limit", "round_limit"]},
               "summary": _TEXT, "remaining_issues": _EVIDENCE, "evidence": _EVIDENCE},
              ["reason", "summary", "remaining_issues", "evidence"]),
]

SYSTEM = ("""You are SceneLoop's visual game-scene agent. Use the supplied Blender tools;
never output Python or pretend an operation occurred. Create, observe, inspect, edit
and review actual results. Preserve user constraints and functional layout. The
comparison mode supplied by the task determines whether an adopted reference is used.
""" + REFERENCE_SCOPE_POLICY + "\n" + SCENE_GEOMETRY_CONTRACT + "\n" + VISUAL_REVIEW_POLICY + """
Create a ScenePlan via create_scene for a new scene, then examine actual returned
images before editing. Continuation tasks supply an existing scene instead. The tools
automatically capture main+auxiliary; observe_scene adds a chosen diagnostic view.
Relevant reference and Before/After images are attached at each turn, labelled with
revision and observation IDs. Evidence must cite IDs actually supplied IN THIS TURN.
Tools never see your unexpressed intentions.
Every inspection result is scene evidence with separate sections: source (run/revision
identity; a legitimately unversioned null differs from unknown), measured (values from
that revision's inspector; the document states the method and field_sources per value),
declared (author/model labels, roles and requested features, never proof),
editable_specification (generated values are authored nominal; imported values are
measured from the file; neither is evaluated geometry) and verification (capability
boundaries). Tool success only means the request executed. Never present a declared
feature, role, name or planned dimension as measured fact, and never claim an opening,
interior, collision or walkability was verified unless it appears in measured.
inspect_geometry reads the actual mesh of one object/part from the frozen .blend:
real counts, optional mesh-local vertex/edge/face arrays, matrix_world and topology
statistics (boundary/wire/non-manifold edges and connected components). It is
read-only and does not consume an Edit Round. Topology facts are not proof that an
opening, interior or collision state is correct; original and evaluated meshes are
different sources and their indexes are not interchangeable. Use part_id when the
object has multiple parts.

Edits contain 1..16 operations: {op:'add',object:complete object},
{op:'remove',id:object ID}, or {op:'update',id,changes:{allowed object fields}}.
Update may change name,kind,position,rotation,scale,dimensions,key_object,features,parts;
id cannot change. Use absolute values. Comparison cameras stay frozen during edits.
Generic compound edits: {op:'add_part',id:object ID,part:complete part with unique id},
{op:'update_part',id:object ID,part_id,changes:{part fields except id}},
{op:'remove_part',id:object ID,part_id}. Shape changes need the new shape's required
geometry fields. Omitted parts/fields remain intact. No whole scene reconstruction;
only explicit geometry replacement affects that target's mesh. Inspect before editing.
Real subtraction on an existing compound part is available as cut_box:
{op:'cut_box',id:object ID,part_id:registered part id,mode:'opening',axis:'X'|'Y'|'Z',
center:[u,v],size:[width,height]} cuts a rectangular channel through the part along
that axis; mode:'recess' also needs side:'min'|'max' and depth and cuts a blind slot
from the bounding-box entrance on that side. axis maps X->(Y,Z), Y->(X,Z), Z->(X,Y).
center/size/depth are in the target part's actual original mesh-local coordinates as
read from inspect_geometry, never plan dimensions or world metres. Up to 4 cut_box
operations per batch, at most 16 operations total. The tool result reports measured
before/after signatures, volume change and local sample checks; success covers only
this bounded local subtraction, not the semantic object or scene quality. A cut part
keeps its identity and materials but becomes protected: replacing its shape
parameters, changing the root kind or overwriting all parts is refused; transforms,
further cut_box edits, edits to other parts and explicit remove/remove_part still
work. Rejections explain why; correct the target or values instead of replacing the
cut with a decorative box.
A diagnostic view is {preset:'main'|'auxiliary'} or
{position:[x,y,z],target:[x,y,z],lens:number,roll_degrees:optional -360..360}.

apply_edit forks the selected revision; the resulting candidate is pending. See its
real Before/After images, then review_candidate before another edit. Better requires
supported improvement and preserved constraints; changing or adding something alone
is not improvement. Essential uncertainty requires unknown preservation and user review.
Keep the original and best supported revision; no generic success scores. Two consecutive
non-improvements stop. Separate visible improvement from unverified game functionality.

Use budgets deliberately; reserve a model call after every edit for review. Budget or
round exhaustion is not goal_reached. Use finish reason round_limit when edit rounds
are exhausted and the stage goal is still unmet. If essential evidence is uncertain,
finish needs_user_review; if shapes cannot be represented, finish capability_limit.
Only goal_reached with actual evidence of the current stage goal; explicitly list
remaining issues and simplifications. Execution completion is not production acceptance.
Never request further image GENERATION: screenshots come from Blender, not the image model.
First infer the user's intent. A question such as "why does this look wrong" requests
inspection and explanation, not an edit. Inspect or observe as needed, then use respond.
An explicit modification may be executed directly. If the target or requested change is
truly ambiguous, use respond(kind='clarify') instead of guessing. Discussion, inspection
and diagnostic renders do not consume an Edit Round.
""")


CONTINUATION_INSTRUCTIONS = """
TASK OVERRIDE: This is a CONTINUATION of an existing project scene. The
initial revision already exists; inspect and edit it, never create a replacement scene.
The design_context is frozen from the original scene. Follow the new edit request
while retaining that intent, style, composition and functionality. The supplied
comparison mode controls evaluation: intent means Before/After improvement ONLY,
without absolute image-reference similarity; reference means the user explicitly
chose the inherited adopted reference. No reference image is supplied in intent mode.
For imported scenes preserve existing geometry/materials/style; do not force white-model
appearance or replace assets with primitives. Read the manifest's editable_operations,
editable_fields and readonly_reasons. Imported static object positions are OBJECT
ORIGINS in scene units, rotations XYZ degrees, scale actual object scale. Template
additions have bottom-center positions. Do not assume imported units are meters:
read unit_scale_length. Only change declared editable fields on existing objects;
unsupported/animated/linked/parented objects may be read-only. Use add only for
bounded supported templates. If the requested asset cannot be edited safely, finish
with capability_limit or needs_user_review and clearly explain why.
"""

LEGACY_WORKING_INSTRUCTIONS = """Each successfully reviewed edit is committed as the project's Working Scene before
you are told it succeeded. Discussion and failed edits leave the Working Scene unchanged."""

LOCAL_ROUND_WORKING_INSTRUCTIONS = """In local_round_v1 each successful review selects only an isolated draft. Working stays
unchanged until commit_local_round validates the whole start-to-final comparison and saves once.
Begin a local round with a real Plan item before editing. After a confirmed draft you may
checkpoint_local_round to pause and explicitly resume this Run. End rejected or uncertain
rounds without publishing. Independent Judge reads only formally saved Working."""


def _builder_system(method, *, editing, local_round_mode):
    """Append the Run-bound method and the explicitly selected continuation rules."""
    system = SYSTEM + ("\n" + method if method is not None else "")
    if editing:
        system += CONTINUATION_INSTRUCTIONS
        system += (LOCAL_ROUND_WORKING_INSTRUCTIONS if local_round_mode
                   else LEGACY_WORKING_INSTRUCTIONS)
    return system


class VisualAgent:
    def __init__(self, manager, identifier, *, resume=False, resource_checkpoint=None, asset_checkpoint=False):
        self.manager, self.identifier = manager, identifier
        self.providers = manager.providers
        self.run = manager.get(identifier)
        self.root = manager.run_directory(identifier)
        self.adapter = manager._adapter_for(self.run)
        self.local_round_mode = local_round_enabled(self.run)
        self.resume = resume or resource_checkpoint is not None
        self.limits = (resolve_snapshot(self.run) if self.local_round_mode
                       else limits_for(self.run["iteration"]["max_rounds"]))
        if self.local_round_mode:
            # The old executor field names a draft-attempt cap, never formal saves.
            self.limits = {**self.limits, "max_rounds": self.limits["max_edit_attempts"]}
        if self.run.get('agent_resource_base'):
            from .agent_resources import overlay
            self.limits = overlay(self.limits, self.run['resource_budget'])
            self.limits['max_rounds'] = self.limits['max_edit_attempts']
        self.state = {"limits": self.limits, "used": {"edit_rounds": 0, "tool_calls": 0, "observations": 0},
                      "selected_revision": None, "pending_revision": None, "stop_reason": None,
                      "revisions": [], "observations": [], "geometry": [], "actions": [], "no_progress_count": 0}
        self.started = time.monotonic()
        self.active_seconds_prior = 0
        if resource_checkpoint is not None:
            if asset_checkpoint:
                host=manager._asset_host();doc=host.verify(manager,identifier)
                if not doc.get('bootstrap') or doc['checkpoint']['continuation_checkpoint'] != resource_checkpoint['id']:
                    raise RoundError('资产检查点不属于原初建Run。','asset_checkpoint_drift',409)
            else:
                from .bootstrap_checkpoint import verify
                verify(manager, self.run, resource_checkpoint)
            self.state = copy.deepcopy(resource_checkpoint['agent_state'])
            self.state['limits'] = self.limits
            self.active_seconds_prior = self.run.get('local_round_active_seconds', 0)
        if resume:
            if not self.local_round_mode:
                raise RoundError("旧模式不支持草稿续做。", "round_not_resumable", 409)
            round_state = manager._projects().state_store(self.run["project_id"]).local_round(identifier)
            if round_state is None or round_state["status"] != "active" or not round_state.get("checkpoint"):
                raise RoundError("续做恢复点不存在。", "round_not_resumable", 409)
            self.state = copy.deepcopy(round_state["agent_state"])
            self.state["limits"] = self.limits
            # An older checkpoint may carry a process-local shared deadline.
            # It has no control effect and must not reach the resumed model.
            self.state.pop("deadline", None)
            self.active_seconds_prior = max(round_state.get("active_seconds_used", 0),
                                            self.run.get("local_round_active_seconds", 0))
        editing = self.run.get("operation") == "edit"
        continuation=self.run.get('director_delegation',{}).get('brief',{}).get('creative_continuation')
        self.tools = [tool for tool in TOOLS if not (editing or continuation) or tool["function"]["name"] != "create_scene"]
        from . import builder_memory
        self.tools.append(builder_memory.TOOL)
        if self.run.get('bootstrap'):
            self.tools += [_function('review_initial','Explicit self-check of the selected initial candidate using both fixed views. This is not an independent Judge pass.',
                {'verdict':{'type':'string','enum':['pass','fail','uncertain']},'reason':_TEXT,
                 'evidence':{'type':'array','items':_TEXT}},['verdict','reason','evidence'])]
        from . import asset_builder
        self.asset_pause = None
        self.resource_boundary = None
        if self.run.get('agent_resource_base'):
            from .agent_resources import TOOLS as RESOURCE_TOOLS
            self.tools += RESOURCE_TOOLS
            if self.run.get('bootstrap'):
                self.tools = [t for t in self.tools if t['function']['name'] != 'save_checkpoint']
        if self.run.get('asset_capability') and (self.local_round_mode or self.run.get('bootstrap')):
            self.tools += asset_builder.TOOLS
        local_tools = {"begin_local_round", "checkpoint_local_round", "commit_local_round",
                       "end_local_round", "judge_working_scene"}
        if not self.local_round_mode:
            self.tools = [t for t in self.tools if t["function"]["name"] not in local_tools]
        if not self.run.get("project_id"):
            self.tools = [t for t in self.tools if t["function"]["name"] not in {"read_refinement_plan", "update_refinement_plan"}]
        from .refinement_method import bind_method, method_content
        if self.run.get("goal_snapshot") and "refinement_method" not in self.run:
            with self.manager.lock:
                stored = self.manager._get(self.identifier)
                stored["refinement_method"] = bind_method(stored)
                self.manager._save(stored)
                self.run = copy.deepcopy(stored)
        system = _builder_system(
            method_content(self.run) if self.run.get("goal_snapshot") else None,
            editing=editing, local_round_mode=self.local_round_mode,
        )
        system += '\n' + builder_memory.INSTRUCTIONS
        if self.run.get('asset_capability'):
            system += '\n' + asset_builder.INSTRUCTIONS
        if self.run.get('agent_resource_base'):
            from .agent_resources import INSTRUCTIONS
            system += '\n' + INSTRUCTIONS
        if self.run.get('director_delegation',{}).get('brief',{}).get('creation_authorization'):
            system += '\nExplicit creative scope supports typed assign_surface (wood/stone/metal/cloth/ground/paint; target object_id/part_id or instance_id/node_id and slot) and configure_lighting (bounded sky/color world and SUN/AREA/POINT). These edit the same candidate and require real appearance observations, self-check, formal save and independent Judge. Use actual measured surface/asset facts; missing data remains unknown. Do not submit scripts, arbitrary materials, texture paths or resource permissions.'
        model_limits = copy.deepcopy(self.limits)
        if self.local_round_mode and self.limits["schema_version"] == 2:
            model_limits.pop("max_rounds", None)
        model_limits.pop("max_seconds", None)
        if "completion_reserve" in model_limits:
            model_limits["completion_reserve"].pop("active_seconds", None)
        self.history = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({
            "request": self.run["prompt"], "constraints": self.run["constraints"], "limits": model_limits,
            "evaluation": self.run.get("evaluation", {"mode": "reference"}),
            "design_context": self.run.get("design_context"), "goal": self.run.get("goal_snapshot")}, ensure_ascii=False)}]
        self.system = system
        self.audit = builder_context.BusinessAudit(self.root)
        builder_memory.validate(self)
        from .builder_recovery import BuilderRecovery
        self.recovery = BuilderRecovery(self)
        if not hasattr(manager, '_builder_recoveries'):
            manager._builder_recoveries = {}
        manager._builder_recoveries[identifier] = self.recovery
        self.tool_results = {}
        self.correction=None
        if getattr(self.providers,'BUILDER_ARGUMENT_CORRECTION',False):
            from .tool_correction import BuilderCorrection
            self.correction=BuilderCorrection(manager,identifier)
            if not hasattr(manager,'_builder_tool_protocols'):manager._builder_tool_protocols={}
            manager._builder_tool_protocols[identifier]=self.correction
        if resume and self.run.get('asset_capability'):
            doc = manager._asset_host().store(manager,identifier).request(identifier)
            if doc and doc.get('builder_mode'):
                asset_builder.restore(self,doc)
        self.seen = set()
        self.seen_revisions = set()
        if resource_checkpoint is not None:
            from .builder_continuation import load
            self.history += load(manager, self.run, resource_checkpoint)
            self.tool_results = copy.deepcopy(resource_checkpoint['tool_results'])
            self.seen = set(resource_checkpoint['seen'])
            self.seen_revisions = set(resource_checkpoint['seen_revisions'])
        reference = self.run.get("reference")
        self.concept = next((item for item in self.run["artifacts"] if reference and item["id"] == reference["artifact_id"]), None)
        self.reference_path = self.root / self.concept["path"] if self.concept else None
        self._check_reference()

    def _check_reference(self):
        if self.reference_path is None:
            if self.run.get("evaluation", {}).get("mode") != "intent":
                raise self.providers.ProviderError("reference_missing", "本次参考模式缺少已采用图片。")
            return
        if file_sha256(self.reference_path) != self.run["reference"]["sha256"]:
            raise self.providers.ProviderError("reference_changed", "采用的参考图发生变化，视觉循环已停止。")

    def _event(self, stage, message, **extra):
        if self.local_round_mode:
            extra["local_round_active_seconds"] = max(
                self.run.get("local_round_active_seconds", 0),
                self.active_seconds_prior + time.monotonic() - self.started)
        self.manager._update(self.identifier, stage, message, agent=copy.deepcopy(self.state), **extra)

    def _trace(self, kind, value):
        # No credentials, image data URLs or private reasoning in persisted traces.
        def scrub(item):
            if isinstance(item, str):
                return re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+", "[image omitted]", item)[:20000]
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, dict):
                return {key: scrub(child) for key, child in item.items() if key != "reasoning_content"}
            return item
        reference = None
        try:
            reference = self.audit.write(kind, value)
            with (self.root / "agent-trace.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "value": scrub(builder_context.scrub(value)),
                                         "audit": reference}, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError):
            status = self.state.setdefault("builder_audit", {"status": "incomplete", "failures": 0})
            status.update(status="incomplete", failures=status.get("failures", 0) + 1, last_kind=kind)
        if kind == "tool":
            builder_context.record_decision(self.state, value, reference)
            builder_context.remember_evidence(self.state, value, reference)
        return reference

    def _check_budget(self, *, observations=0, tool=False, edit=False):
        if self.manager._cancelled(self.identifier):
            raise LoopStopped("cancelled", "已停止后续观察和编辑；已有版本保留。")
        # A committed Edit Round advances the project compare-and-swap revision.
        # Refresh the run's moving baseline before the next tool or model call.
        self.run = self.manager.get(self.identifier)
        self.manager._verify_project_baseline(self.run)
        if self.run.get('agent_resource_base'):
            from .agent_resources import overlay
            # The next round must lose its predecessor's one-time draft grant.
            self.limits = (resolve_snapshot(self.run) if self.local_round_mode
                           else overlay(self.limits, self.run['resource_budget']))
            self.limits['max_rounds'] = self.limits['max_edit_attempts']
            self.state['limits'] = self.limits
            if edit and self.run.get('bootstrap'):
                if (self.run['usage']['brain_calls'] >= self.limits['max_brain_calls'] - 2
                        or self.state['used']['tool_calls'] >= self.limits['max_tool_calls'] - 3):
                    raise ToolInputError('剩余额度预留给自检、完成或request_resources；请先申请资源。')
        if self.local_round_mode and self.limits["schema_version"] == 2 and edit:
            progress = budget_progress(
                self.run, self.manager._projects().state_store(self.run["project_id"]),
                agent_state=self.state,
                active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
            blocker = edit_limit(progress, observations=observations)
            if blocker:
                raise RoundError(f"草稿编辑预算不足：{blocker}；已确认草稿仍可自检或提交。",
                                 "round_resource_exhausted", 409)
        if self.state["used"]["observations"] + observations > self.limits["max_observations"]:
            if self.run.get('agent_resource_base'):
                raise ToolInputError('观察额度不足，本次观察尚未执行；可用request_resources明确申请observations。')
            raise LoopStopped("observation_limit", "本轮截图额度已用完，保留已观察版本。")
        if tool and self.state["used"]["tool_calls"] >= self.limits["max_tool_calls"]:
            raise LoopStopped("tool_limit", "本轮工具调用额度已用完。")
        if edit and self.state["used"]["edit_rounds"] >= self.limits["max_rounds"]:
            if self.run.get('agent_resource_base'):
                raise ToolInputError('草稿尝试额度已耗尽；可申请明确的draft_attempts增量，不能重放旧操作。')
            raise LoopStopped("round_limit", "已达到草稿尝试上限；已确认草稿仍可独立完成轮末提交。" if self.local_round_mode else
                              "达到最多修改轮数，保留已评审的版本。")

    def _revision(self, identifier):
        for revision in self.state["revisions"]:
            if revision["id"] == identifier:
                return revision
        raise ToolInputError("场景版本不存在；请使用工具返回的 revision_id。")

    def _workspace(self, revision):
        return self.root / "candidates" / revision["id"]

    def _verify_revision(self, revision):
        registered = self.manager.get(self.identifier)["artifacts"]
        for artifact in registered:
            if artifact.get("revision_id") == revision["id"] and artifact["kind"] in {"blend", "plan", "inspection", "render"}:
                path = (self.root / artifact["path"]).resolve()
                if not path.is_relative_to(self.root) or not path.is_file() or file_sha256(path) != artifact["sha256"]:
                    if artifact["kind"] == "render":
                        raise self.providers.ProviderError("observation_changed", "已登记的观察图片发生变化，停止比较。")
                    raise self.providers.ProviderError("revision_changed", "冻结版本的场景或计划已被修改，循环已停止。请保留手动修改；本轮不能沿原基线继续。")

    def _args(self, arguments, keys):
        if not isinstance(arguments, dict) or set(arguments) != set(keys):
            raise ToolInputError("工具参数字段不匹配。")

    def _text(self, value, maximum=2000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ToolInputError("需要非空且长度受限的说明。")
        return value.strip()

    def _validate(self, function, *arguments):
        try:
            return function(*arguments)
        except self.manager.blender.BlenderError as error:
            raise ToolInputError(str(error)) from None

    def _register(self, path, kind, revision, **metadata):
        item = self.manager._artifact(self.identifier, path, kind, revision_id=revision["id"], **metadata)
        revision["artifact_ids"].append(item["id"])
        return item

    def _reserve_observations(self, count, *, optional=False):
        if optional and self.local_round_mode and self.limits["schema_version"] == 2:
            progress = budget_progress(
                self.manager.get(self.identifier),
                self.manager._projects().state_store(self.run["project_id"]),
                agent_state=self.state,
                active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
            if (progress["phase"] == "editing" and
                    progress["remaining"]["observations"] - count < 2):
                raise RoundError("可选观察不能耗尽下一次草稿的固定双视角额度。",
                                 "round_resource_exhausted", 409)
        self._check_budget(observations=count)
        self.state["used"]["observations"] += count
        self._event("observing", f"正在观察候选；已占用 {self.state['used']['observations']}/{self.limits['max_observations']} 张截图额度。")

    def _available_tools(self):
        """Keep the last calls for a review, formal save, or safe exit."""
        if self.run.get('agent_resource_base') and self.run.get('bootstrap'):
            used = self.state['used']
            if (self.run['usage']['brain_calls'] >= self.limits['max_brain_calls'] - 2
                    or used['tool_calls'] >= self.limits['max_tool_calls'] - 3
                    or used['observations'] + 2 > self.limits['max_observations']
                    or used['edit_rounds'] >= self.limits['max_rounds']):
                names = {'review_initial', 'review_candidate', 'finish', 'checkpoint_candidate', 'request_resources'}
                return [t for t in self.tools if t['function']['name'] in names]
        if not self.local_round_mode or self.limits["schema_version"] != 2:
            return self.tools
        # Most calls are far from every reserve. Avoid a second SQL projection
        # for them. self.run is the count before this turn's brain reservation,
        # while the full projection below reads the reserved count, so the fast
        # path keeps one explicit extra brain call. The observation pair matches
        # the full gate's fixed two-image check.
        used = self.state["used"]
        reserve = self.limits["completion_reserve"]
        upcoming_brain_call = 1
        observation_pair = 2
        if (self.run["usage"]["brain_calls"] < self.limits["max_brain_calls"]
                - (reserve["brain_calls"] + upcoming_brain_call)
                and used["tool_calls"] < self.limits["max_tool_calls"] - reserve["tool_calls"]
                and used["observations"] <= self.limits["max_observations"] - observation_pair):
            return self.tools
        run = self.manager.get(self.identifier)
        store = self.manager._projects().state_store(run["project_id"])
        progress = budget_progress(
            run, store, agent_state=self.state,
            active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
        remaining, reserve = progress["remaining"], progress["limits"]["completion_reserve"]
        observation_pair = 2
        scarce_completion_resource = (remaining["brain_calls"] <= reserve["brain_calls"]
                                      or remaining["tool_calls"] <= reserve["tool_calls"]
                                      or remaining["observations"] < observation_pair)
        if not scarce_completion_resource:
            return self.tools
        document = self.manager.get_local_round(self.identifier)
        names = {"respond", "checkpoint_candidate", "request_resources"}
        if document and document["status"] in {"active", "paused"}:
            if document.get("pending_revision"):
                names.add("review_candidate")
            else:
                names.add("end_local_round")
                if document.get("selected_revision") != document["start"]["revision_id"]:
                    names.update({"commit_local_round", "checkpoint_local_round"})
        else:
            names.update({"finish", "judge_working_scene"})
        return [tool for tool in self.tools if tool["function"]["name"] in names]

    @staticmethod
    def _observation_identity(result, view=None):
        try:
            return copy.deepcopy(validate_observation_metadata(result, view))
        except ValueError as error:
            from .providers import ProviderError
            raise ProviderError('observation_identity_invalid', '实际观察返回的证据身份无效；保留已执行状态并停止。') from None

    def _capture_pair(self, revision):
        self._reserve_observations(2)
        workspace = self._workspace(revision)
        rendered = self.adapter.render(workspace) or {}
        for view in ("main", "auxiliary"):
            actual = self._observation_identity(rendered.get("observations", {}).get(view, {}),
                                                {"preset": view})
            observation = {"id": f"{revision['id']}-{view}", "revision_id": revision["id"],
                           "view_id": view, "view": {"preset": view}, "comparison": True}
            observation.update(actual)
            item = self._register(workspace / (view + ".png"), "render", revision,
                                  view_id=view, observation_id=observation["id"],
                                  observation=actual)
            observation["artifact_id"] = item["id"]
            observation["path"] = item["path"]
            observation["sha256"] = item["sha256"]
            self.state["observations"].append(observation)
            cache=self._shared_cache()
            if cache:
                source=cache.source(workspace,{'preset':view})
                record=cache.publish(source,self.identifier,revision['id'],item,actual)
                cache.builder_rendered(self.run,record,item['size'])
        self._register(workspace / "inspection.json", "inspection", revision)

    def _shared_cache(self):
        if not self.run.get('director_delegation'): return None
        store=self.manager._director_host().store(self.run['project_id'])
        binding=store.child(self.identifier)
        if not binding or not store.session(binding['parent_id']).get('observations'): return None
        from .observation_service import ObservationCache
        return ObservationCache(self.manager,self.run['project_id'],self.adapter)

    @staticmethod
    def _camera_summary(camera):
        if not isinstance(camera, dict):
            return None
        # Do not repeat Blender UI flags, custom camera code/paths and irrelevant
        # panorama settings in every model turn. Actual identity is kept separately.
        keys = ("position", "target", "matrix_world", "type", "lens", "ortho_scale",
                "sensor_fit", "sensor_width", "sensor_height", "shift_x", "shift_y",
                "clip_start", "clip_end", "resolution", "resolution_percentage", "engine", "samples")
        return {key: copy.deepcopy(camera[key]) for key in keys if key in camera}

    @classmethod
    def _public_observation(cls, observation):
        result = copy.deepcopy(observation)
        if "camera" in result:
            result["camera"] = cls._camera_summary(result["camera"])
        return result

    def _evidence_source(self, revision, *, working_version=None):
        run = self.manager.get(self.identifier)
        source = {"run_id": self.identifier, "revision_id": revision["id"],
                  "working_version": working_version, "working_scene": working_version is not None,
                  "artifacts": artifact_refs(run, revision["id"])}
        if revision.get("source_scene"):
            source["base_selection"] = copy.deepcopy(revision["source_scene"])
        if revision.get("parent_id"):
            source["base_revision"] = revision["parent_id"]
        if working_version is None:
            source["note"] = "这是本轮候选；Working Version 未声明。"
        return source

    def _build_evidence(self, revision, *, object_ids=None, detail="summary"):
        workspace = self._workspace(revision)
        plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        inspection = json.loads((workspace / "inspection.json").read_text(encoding="utf-8"))
        return build_scene_evidence(plan, inspection, source=self._evidence_source(
            revision, working_version=revision.get("working_version")), object_ids=object_ids, detail=detail)

    @staticmethod
    def _cut_measurements(inspection, operations):
        """Bounded host measurements for this batch's cut_box targets.

        Always assembled from the finally published inspection of the saved
        candidate: the same record that produced the evidence document, so the
        tool payload can never show this part as both current and historical.
        """
        targets = {(item.get("id"), item.get("part_id")) for item in operations
                   if isinstance(item, dict) and item.get("op") == "cut_box"}
        if not targets:
            return []
        rows = inspection.get("semantic_objects") if isinstance(inspection, dict) else None
        summaries = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            for part in row.get("parts") if isinstance(row.get("parts"), list) else []:
                if not isinstance(part, dict) or (row.get("id"), part.get("part_id")) not in targets:
                    continue
                derived = part.get("derived_geometry")
                if not isinstance(derived, dict):
                    continue
                receipt = derived.get("receipt") if isinstance(derived.get("receipt"), dict) else {}
                record = {
                    "object_id": row.get("id"), "part_id": part.get("part_id"),
                    "status": derived.get("status") if derived.get("status") in ("current", "historical", "invalid")
                    else "invalid",
                    "current": bool(derived.get("valid")), "reason": derived.get("reason"),
                    "operation": copy.deepcopy(receipt.get("operation"))}
                if record["current"]:
                    record.update({
                        "before": copy.deepcopy(receipt.get("before")),
                        "after": copy.deepcopy(receipt.get("after")),
                        "entry_checks": copy.deepcopy(receipt.get("entry_checks")),
                        "tolerance_used": copy.deepcopy(receipt.get("tolerance_used")),
                        "coordinates": receipt.get("coordinates"), "method": receipt.get("method"),
                        "limitations": copy.deepcopy(receipt.get("limitations"))})
                else:
                    record["note"] = "该保存文件上的核验记录不是当前有效测量；仅作来源说明。"
                summaries.append(record)
        return summaries

    def _publish(self, revision):
        workspace = self._workspace(revision)
        self._register(workspace / "plan.json", "plan", revision)
        self._register(workspace / "scene.blend", "blend", revision)
        self._capture_pair(revision)
        return {"revision": self._public_revision(revision), "inspection": self._build_evidence(revision),
                "observations": [self._public_observation(o) for o in self.state["observations"] if o["revision_id"] == revision["id"]]}

    @staticmethod
    def _public_revision(revision):
        return {key: copy.deepcopy(value) for key, value in revision.items() if key != "artifact_ids"}

    def _create(self, arguments):
        self._args(arguments, ["plan"])
        if self.state["revisions"]:
            raise ToolInputError("已有初始场景；请使用 apply_edit 创建修改候选。")
        data=copy.deepcopy(arguments['plan'])
        creative=self.run.get('director_delegation',{}).get('brief',{}).get('creation_authorization',{}).get('approved',{}).get('creation') or {}
        if self.run.get('bootstrap') and creative.get('review_stage','appearance')=='appearance' and isinstance(data,dict):
            data.setdefault('render_profile','showcase_v1')
        plan = self._validate(self.manager.blender.validate_plan, data)
        self._check_budget(observations=2)
        revision = {"id": "r000", "parent_id": None, "round": 0, "status": "selected", "artifact_ids": []}
        if self.run.get("project_id"):
            self.manager._scene_operation(self.identifier, revision["id"], "initial")
        workspace = self.adapter.create_workspace(self._workspace(revision))
        self.adapter.execute_plan(plan, workspace)
        result = self._publish(revision)
        self.state["revisions"].append(revision)
        self.state["selected_revision"] = revision["id"]
        self._event("reviewing", "初版候选已生成；将实际主辅视角送回大脑检查。", round=0)
        return result

    def _review_initial(self, arguments):
        self._args(arguments,['verdict','reason','evidence'])
        if not self.run.get('bootstrap') or self.state['pending_revision']:
            raise ToolInputError('只有初建选定候选可接受初版自检。')
        selected=self.state['selected_revision']
        if selected is None or arguments['verdict'] not in {'pass','fail','uncertain'}:
            raise ToolInputError('初版自检需要选定候选及pass/fail/uncertain。')
        reason=self._text(arguments['reason'])
        self._evidence(arguments['evidence'],[selected])
        views={o['view_id'] for o in self.state['observations']
               if o['revision_id']==selected and o['id'] in arguments['evidence'] and o['comparison']}
        if not {'main','auxiliary'}<=views:raise ToolInputError('初版自检需要实际主辅固定观察。')
        self._verify_revision(self._revision(selected))
        result={'revision_id':selected,'verdict':arguments['verdict'],'reason':reason,
                'evidence':copy.deepcopy(arguments['evidence']),
                'artifacts':self.manager._fixed_scene_artifacts(self.manager.get(self.identifier),selected)}
        self.state['initial_self_check']=result
        self._revision(selected)['assessment']=copy.deepcopy(result)
        return copy.deepcopy(result)

    def _inspect(self, arguments):
        if not isinstance(arguments, dict) or "revision_id" not in arguments or set(arguments) - {"revision_id", "object_ids"}:
            raise ToolInputError("检查需要 revision_id 和可选 object_ids。")
        requested = arguments.get("object_ids")
        if requested is not None and (not isinstance(requested, list) or not 1 <= len(requested) <= 32 or any(not isinstance(x, str) for x in requested)):
            raise ToolInputError("object_ids 需要 1–32 个物体 ID。")
        revision = self._revision(arguments["revision_id"])
        self._verify_revision(revision)
        # Stored inspection belongs to the frozen revision; reading cannot overwrite its artifact.
        plan = json.loads((self._workspace(revision) / "plan.json").read_text(encoding="utf-8"))
        inspection = json.loads((self._workspace(revision) / "inspection.json").read_text(encoding="utf-8"))
        if requested is not None:
            if set(requested) - set(known_object_ids(plan, inspection)):
                raise ToolInputError("object_ids 包含该版本中不存在的物体。")
            result = {"revision_id": revision["id"], "inspection": self._build_evidence(
                revision, object_ids=requested, detail="full")}
            result["detail_access"] = "focused inspection keeps complete measured parts and complete editable plan parameters for the requested objects."
            return result
        return {"revision_id": revision["id"], "inspection": self._build_evidence(revision),
                "detail_access": "Summary evidence. Call inspect_scene with object_ids for complete measured parts and editable plan parameters of a target object."}

    def _geometry(self, arguments):
        try:
            request = validate_builder_request(arguments)
        except ValueError as error:
            raise ToolInputError(str(error)) from None
        revision = self._revision(request["revision_id"])
        self._verify_revision(revision)
        try:
            measurement = self.adapter.inspect_geometry(
                self._workspace(revision), request["object_id"],
                request.get("part_id"), request["representation"])
        except self.manager.blender.BlenderError as error:
            if getattr(error, 'outcome', None) != 'rejected':raise
            raise ToolResultError(str(error), details={'execution':'executed_read_rejected'}) from None
        self._verify_revision(revision)
        identity = source_identity({
            "run_id": self.identifier, "revision_id": revision["id"],
            "working_version": revision.get("working_version"),
            "working_scene": revision.get("working_version") is not None,
            "artifacts": artifact_refs(self.manager.get(self.identifier), revision["id"]),
            "base_revision": revision.get("parent_id"),
        }, kind=(measurement.get("source") or {}).get("scene_kind", "unknown"))
        evidence = geometry_evidence_id(revision_id=revision["id"], object_id=request["object_id"],
                                        part_id=request.get("part_id"), representation=request["representation"],
                                        session=self.identifier[:8])
        document = finalize_document(measurement, identity=identity, evidence_id_value=evidence)
        mesh = document.get("mesh", {})
        record = {"id": evidence, "revision_id": revision["id"], "object_id": request["object_id"],
                  "part_id": request.get("part_id"), "representation": request["representation"],
                  "blend_sha256": document["source"].get("blend_sha256"),
                  "vertex_count": mesh.get("vertex_count"), "edge_count": mesh.get("edge_count"),
                  "polygon_count": mesh.get("polygon_count")}
        records = [row for row in self.state.get("geometry", []) if row.get("id") != evidence]
        records.append(record)
        self.state["geometry"] = records
        self.seen.add(evidence)
        self._event("agent_tool", f"已读取 {revision['id']} / {request['object_id']}"
                                  f"{'/' + request['part_id'] if request.get('part_id') else ''} 的实际网格"
                                  f"（{request['representation']}）：{mesh.get('vertex_count')} 顶点 / "
                                  f"{mesh.get('edge_count')} 边 / {mesh.get('polygon_count')} 面。")
        return document

    def _observe(self, arguments):
        self._args(arguments, ["revision_id", "view"])
        requested = self._revision(arguments["revision_id"])
        if requested["id"] not in {self.state["selected_revision"], self.state["pending_revision"]}:
            raise ToolInputError("请观察当前保留或待比较版本；历史版本可以在界面查看。")
        view = self._validate(self.manager.blender.validate_view_spec, arguments["view"])
        if self.state["pending_revision"]:
            pair = [self.state["selected_revision"], self.state["pending_revision"]]
            if requested["id"] not in pair:
                raise ToolInputError("正在比较当前 Before/After；请从该对版本选择观察。")
            revisions = [self._revision(item) for item in pair]
        else:
            revisions = [requested]
        for revision in revisions:
            self._verify_revision(revision)
        cache=self._shared_cache()
        sources=[cache.source(self._workspace(revision),view) for revision in revisions] if cache else [None]*len(revisions)
        hits=[cache.find(source) for source in sources] if cache else [None]*len(revisions)
        self._reserve_observations(sum(hit is None for hit in hits), optional=True)
        group = f"diagnostic-{len(self.state['observations']) if cache else self.state['used']['observations']:03d}"
        observations = []
        for revision,source,hit in zip(revisions,sources,hits):
            self._check_budget()
            output_id = f"{group}-{revision['id']}"
            if hit:
                cache.consume(self.run,hit,hit=True,consumer_tag=output_id)
                observation={'id':output_id,'revision_id':revision['id'],'view_id':group,'view':view,
                    'comparison':len(revisions)==2,'artifact_id':hit['ref']['artifact_id'],'sha256':hit['ref']['sha256'],
                    'shared_ref':hit['ref'],'cache_key':hit['request_key'],'cache_hit':True,**hit['observation']}
                self.state['observations'].append(observation); observations.append(observation)
                continue
            try:
                result = self.adapter.observe(self._workspace(revision), view, output_id)
            except self.manager.blender.BlenderError as error:
                if error.outcome == "rejected" and error.code == "structure_observation_unsupported":
                    raise ToolResultError(str(error), details={'execution':'executed_read_rejected'}) from None
                raise
            actual = self._observation_identity(result, view)
            image_path = result.get("image_path", result.get("path"))
            item = self._register(image_path, "render", revision, view_id=group, observation_id=output_id, observation=actual)
            observation = {"id": output_id, "revision_id": revision["id"], "view_id": group,
                           "view": view, "camera": result.get("camera"), "comparison": len(revisions) == 2,
                           "artifact_id": item["id"], "path": item["path"], "sha256": item["sha256"]}
            observation.update(actual)
            self.state["observations"].append(observation)
            observations.append(observation)
            if cache:
                record=cache.publish(source,self.identifier,revision['id'],item,actual)
                cache.builder_rendered(self.run,record,item['size'])
        self._event("reviewing", "诊断视角已就绪；前后比较使用相同观察参数。")
        return {"observations": [self._public_observation(o) for o in observations]}

    def _edit(self, arguments):
        self._args(arguments, ["base_revision", "reason", "operations"])
        reason = self._text(arguments["reason"])
        if self.state["pending_revision"]:
            raise ToolInputError("先看图并评审当前待定候选，再进行下一轮编辑。")
        if arguments["base_revision"] != self.state["selected_revision"]:
            raise ToolInputError("编辑基线不是当前保留版本，已阻止修改旧版本。")
        base = self._revision(arguments["base_revision"])
        self._verify_revision(base)
        if base["id"] not in self.seen_revisions:
            raise ToolInputError("先查看该基线的真实截图，再决定编辑。")
        self._check_budget(observations=2, edit=True)
        used = self.manager.get(self.identifier)["usage"]["brain_calls"]
        if used >= self.limits["max_brain_calls"]:
            raise LoopStopped("brain_limit", "剩余额度不足以评审修改，保留当前已检查版本。")
        edit_plan = {"reason": reason, "operations": arguments["operations"]}
        before_plan = json.loads((self._workspace(base) / "plan.json").read_text(encoding="utf-8"))
        validator = (self.manager.blender.validate_imported_edit_plan if before_plan.get("scene_kind") == "imported"
                     else self.manager.blender.apply_edit_plan)
        self._validate(validator, before_plan, edit_plan)
        number = (self.manager._projects().state_store(self.run["project_id"]).local_edit_attempt_count(
                  self.identifier) + 1 if self.local_round_mode and self.limits["schema_version"] == 2
                  else next(r['used'] for r in self.run['resource_budget']['resources'] if r['resource'] == 'draft_attempts' and r['scope'] == 'run') + 1
                  if self.run.get('agent_resource_base') and self.run.get('bootstrap') else self.state["used"]["edit_rounds"] + 1)
        if not self.local_round_mode:
            self.state["used"]["edit_rounds"] = number
        revision = {"id": f"r{number:03d}", "parent_id": base["id"], "round": number,
                    "status": "pending", "edit_reason": reason,
                    "operations": copy.deepcopy(edit_plan["operations"]), "artifact_ids": []}
        actions = []
        fields = {"position": "位置", "rotation": "旋转", "scale": "缩放", "dimensions": "尺寸", "kind": "形状", "parts": "部件", "name": "名称", "features": "特征描述", "key_object": "关键物体标记"}
        for operation in edit_plan["operations"]:
            if operation["op"] == "add":
                actions.append("新增 " + operation["object"]["id"])
            elif operation["op"] == "remove":
                actions.append("移除 " + operation["id"])
            elif operation["op"] in {"add_part", "update_part", "remove_part"}:
                label = {"add_part": "新增部件", "update_part": "修改部件", "remove_part": "移除部件"}[operation["op"]]
                part_id = operation.get("part_id", operation.get("part", {}).get("id"))
                actions.append(f"{label} {operation['id']}/{part_id}")
            elif operation["op"] == "cut_box":
                label = "贯穿孔" if operation.get("mode") == "opening" else "盲槽"
                actions.append(f"{label} {operation['id']}/{operation['part_id']}")
            elif operation['op']=='assign_surface':
                target=operation['target']
                actions.append('配置表面 '+str(target.get('object_id') or target.get('instance_id')))
            elif operation['op']=='configure_lighting':
                actions.append('配置场景灯光与环境')
            else:
                changed = "、".join(fields[key] for key in operation["changes"])
                actions.append(f"调整 {operation['id']} 的{changed}")
        self._event("agent_tool", f"第 {number} 轮：从 {base['id']} 创建独立候选；" + "；".join(actions) + "。原因：" + reason, round=number)
        if self.local_round_mode:
            operation = self.manager.start_local_step(self.identifier, revision["id"], base["id"], edit_plan)
            attempt_id = operation["attempt_id"]
            revision["step_id"] = attempt_id
            self.state["used"]["edit_rounds"] = number
        else:
            operation = self.manager._scene_operation(
                self.identifier, revision["id"],
                "builder" if self.run.get("operation") == "edit" else "initial")
            attempt_id = None
            if operation is not None:
                attempt_id = hashlib.sha256((operation["operation_id"] + ":attempt:0").encode()).hexdigest()
                self.manager._projects().state_store(self.run["project_id"]).begin_attempt(
                    operation["operation_id"], attempt_id)
        try:
            self.adapter.fork_candidate(self._workspace(base), self._workspace(revision))
            self._verify_revision(base)
            self._check_budget()
        except Exception as error:
            if attempt_id is not None:
                integrity = getattr(error, "code", None) in {
                    "revision_changed", "observation_changed"}
                self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                    attempt_id, outcome="failed" if integrity else "unknown",
                    failure_category="integrity_failure" if integrity else "write_unknown",
                    detail=str(error)[:500])
                if self.local_round_mode:
                    self.manager.finish_local_step(self.identifier, attempt_id,
                        status="failed" if integrity else "unknown", detail=error)
            raise
        try:
            applied = self.adapter.apply_edit(edit_plan, self._workspace(revision))
        except self.manager.blender.BlenderError as error:
            outcome = getattr(error, "outcome", "unknown")
            if outcome == "rejected":
                if attempt_id is not None:
                    self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                        attempt_id, outcome="failed", failure_category="confirmed_rejected",
                        confirmed_no_commit=True, detail=str(error))
                    if self.local_round_mode:
                        self.manager.finish_local_step(self.identifier, attempt_id,
                                                       status="failed", detail=error)
                raise ToolResultError(str(error), details={'execution':'adapter_rejected',
                    'operation_id':operation.get('operation_id') if operation else None,
                    'attempt_id':attempt_id,'confirmed_no_commit':True}) from None
            if outcome == "not_scheduled":
                # The write request was never handed to Blender: this is an
                # operational stop, not a parameter the model can fix.
                if attempt_id is not None:
                    self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                        attempt_id, outcome="failed", failure_category="confirmed_no_write",
                        confirmed_no_commit=True, detail=str(error))
                    if self.local_round_mode:
                        self.manager.finish_local_step(self.identifier, attempt_id,
                                                       status="failed", detail=error)
                error.scheduled = False
                error.forked_candidate = {"run_id": self.identifier, "revision_id": revision["id"],
                                          "workspace": str(self._workspace(revision))}
                self.state["not_scheduled"] = copy.deepcopy(error.forked_candidate)
                self._event("agent_tool", "Blender 写请求未发出，未执行任何写操作；本轮停止，等待环境恢复。", round=number)
                raise error
            # The write may have happened. Stop every later model/write step and
            # preserve the candidate path for manual verification; never retry.
            pending = {"run_id": self.identifier, "revision_id": revision["id"],
                       "workspace": str(self._workspace(revision)),
                       "operations": copy.deepcopy(edit_plan["operations"]), "detail": str(error)}
            self.state["uncertain_write"] = copy.deepcopy(pending)
            if attempt_id is not None:
                self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                    attempt_id, outcome="unknown", failure_category="write_unknown", detail=str(error))
                if self.local_round_mode:
                    self.manager.finish_local_step(self.identifier, attempt_id,
                                                   status="unknown", detail=error)
            self._event("agent_tool", "Blender 写操作结果未知；候选保留待人工核实，本轮停止后续模型与写操作。", round=number)
            failure = self.manager.blender.BlenderError(
                "Blender 写操作结果未知：候选可能已部分或全部写入，已停止本轮后续模型与写操作；"
                "未自动采纳或重试，请人工核实候选后重开任务。",
                outcome="unknown", code="write_result_unknown")
            failure.pending_candidate = pending
            raise failure
        if attempt_id is not None and not self.local_round_mode:
            self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                attempt_id, outcome="succeeded")
        auxiliary = {key: str(value)[:200] for key, value in
                     (applied or {}).items() if key in ("host_log_error", "host_cleanup_error")}
        if auxiliary:
            self.state.setdefault("auxiliary_errors", []).append(
                {"revision_id": revision["id"], "codes": sorted(auxiliary), "details": auxiliary})
            self._event("agent_tool", "Blender 已确认本次写入成功；宿主辅助 I/O 失败已记录（不影响候选）。",
                        round=number)
        result = self._publish(revision)
        inspection = json.loads((self._workspace(revision) / "inspection.json").read_text(encoding="utf-8"))
        measurements = self._cut_measurements(inspection, edit_plan["operations"])
        if measurements:
            result["cut_measurements"] = measurements
        self.state["revisions"].append(revision)
        self.state["pending_revision"] = revision["id"]
        self.state["actions"].append({"revision_id": revision["id"], "reason": reason,
                                      "operations": copy.deepcopy(edit_plan["operations"]), "status": "pending"})
        if self.local_round_mode:
            self.manager._projects().state_store(self.run["project_id"]).record_attempt_result(
                attempt_id, outcome="succeeded")
            self.manager.finish_local_step(self.identifier, attempt_id, status="pending",
                                           agent_state=self.state)
        self._event("reviewing", f"第 {number} 轮截图已生成，等待大脑比较前后效果。")
        return result

    def _evidence(self, value, revisions, *, allow_geometry=False):
        if (not isinstance(value, list) or not 1 <= len(value) <= 12
                or any(not isinstance(item, str) or item not in self.seen for item in value)):
            raise ToolInputError("评价必须引用本回合实际收到的 observation_id 或已取得的 geometry evidence ID。")
        observed = {o["revision_id"] for o in self.state["observations"] if o["id"] in value}
        if allow_geometry:
            records = {row["id"]: row for row in self.state.get("geometry", [])}
            for item in value:
                record = records.get(item)
                if record is None:
                    continue
                if record.get("revision_id") not in revisions:
                    raise ToolInputError("几何证据来自其他版本，不能作为当前版本事实；请引用当前版本的测量或真实截图。")
                observed.add(record["revision_id"])
        if not set(revisions).issubset(observed):
            raise ToolInputError("评价证据必须同时覆盖要比较的版本；比较仍需要真实 Before/After 截图。")

    def _review(self, arguments):
        self._args(arguments, ["candidate_id", "verdict", "preservation", "reason", "evidence"])
        candidate = self.state["pending_revision"]
        if not candidate or arguments["candidate_id"] != candidate:
            raise ToolInputError("没有匹配的待评审候选。")
        verdict, preservation = arguments["verdict"], arguments["preservation"]
        if not isinstance(verdict, str) or not isinstance(preservation, str) or verdict not in {"better", "equivalent", "worse", "uncertain"} or preservation not in {"pass", "violation", "unknown"}:
            raise ToolInputError("无效的前后比较结论。")
        reason = self._text(arguments["reason"])
        self._verify_revision(self._revision(candidate))
        self._verify_revision(self._revision(self.state["selected_revision"]))
        self._evidence(arguments["evidence"], [candidate, self.state["selected_revision"]])
        paired = {}
        for observation in self.state["observations"]:
            if observation["id"] in arguments["evidence"] and observation["comparison"]:
                # New observations require actual pose/render identities. Legacy records
                # can pair only with other legacy records sharing the frozen view_id.
                try:
                    key = (observation["view_id"],) + comparison_identity(observation)
                except ValueError:
                    continue
                paired.setdefault(key, set()).add(observation["revision_id"])
        if not any({candidate, self.state["selected_revision"]}.issubset(versions) for versions in paired.values()):
            raise ToolInputError("比较证据至少需要一组相同实际相机与渲染设置的 Before/After 截图。")
        revision = self._revision(candidate)
        revision["assessment"] = {"verdict": verdict, "preservation": preservation, "reason": reason,
                                  "evidence": arguments["evidence"], "reviewer": "model"}
        from .run_handoff import record_assessment
        record_assessment(self.state, candidate, revision["assessment"])
        if verdict == "uncertain" or preservation == "unknown":
            if self.local_round_mode:
                revision["status"] = "needs_user_review"
                self.state["pending_revision"] = None
                next(item for item in self.state["actions"] if item["revision_id"] == candidate)["status"] = "not_committed"
                self.manager.review_local_step(self.identifier, revision["step_id"],
                                               revision["assessment"], agent_state=self.state)
            raise LoopStopped("needs_user_review", "大脑对改进或保持项无法确定，已保留原版本和待定候选。")
        if verdict == "better" and preservation == "pass":
            committed = None
            if self.run.get("operation") == "edit" and not self.local_round_mode:
                committed = self.manager.commit_agent_revision(
                    self.identifier, candidate, revision["edit_reason"], revision["operations"])
            self._revision(self.state["selected_revision"])["status"] = "superseded"
            revision["status"] = "selected"
            if committed is not None:
                revision["working_version"] = committed["workspace"]["working_version"]
            self.state["selected_revision"] = candidate
            self.state["no_progress_count"] = 0
            next(item for item in self.state["actions"] if item["revision_id"] == candidate)["status"] = (
                "draft_confirmed" if self.local_round_mode else "modified_pending_verification")
        else:
            revision["status"] = "rejected"
            self.state["no_progress_count"] += 1
            next(item for item in self.state["actions"] if item["revision_id"] == candidate)["status"] = "not_committed"
        self.state["pending_revision"] = None
        if self.local_round_mode:
            self.manager.review_local_step(self.identifier, revision["step_id"],
                                           revision["assessment"], agent_state=self.state)
        self._event("reviewing", f"候选 {candidate} 的比较结论：{verdict}；当前保留 {self.state['selected_revision']}。")
        if not self.local_round_mode and self.state["no_progress_count"] >= 2:
            raise LoopStopped("no_progress", "连续两轮未得到有证据支持的改善，保留之前的版本。")
        result = {"selected_revision": self.state["selected_revision"],
                  "assessment": revision["assessment"],
                  "remaining_rounds": self.limits["max_rounds"] - self.state["used"]["edit_rounds"]}
        if self.local_round_mode:
            result.update(draft_only=True, working_unchanged=True)
        return result

    def _respond(self, arguments):
        self._args(arguments, ["kind", "message", "evidence"])
        if arguments["kind"] not in {"answer", "clarify"}:
            raise ToolInputError("讨论响应类型无效。")
        message = self._text(arguments["message"], 6000)
        evidence = arguments["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 12:
            raise ToolInputError("讨论证据格式无效。")
        if evidence:
            selected = self.state.get("selected_revision")
            if selected is None:
                raise ToolInputError("没有场景时不能引用场景观察。")
            self._evidence(evidence, [selected], allow_geometry=True)
        self.state["model_summary"] = message
        self.state["response_kind"] = arguments["kind"]
        self.state["response_evidence"] = copy.deepcopy(evidence)
        raise LoopStopped("clarification" if arguments["kind"] == "clarify" else "discussion", message)

    def _checkpoint(self, arguments):
        self._args(arguments, ["title"])
        if self.run.get('bootstrap'):
            raise ToolInputError('初建尚无Working检查点；使用checkpoint_candidate保存候选且不结束。')
        title = self._text(arguments["title"], 200)
        project = self.manager.get_project(self.run["project_id"])
        result = self.manager.workspace_action(self.run["project_id"], "checkpoint",
                                               {"title": title, "expected_revision": project["revision"]}, _run_id=self.identifier)
        checkpoint = result["checkpoint"]
        self.state["checkpoint"] = {"id": checkpoint["id"], "title": checkpoint["title"],
                                    "working_version": checkpoint["working_version"]}
        raise LoopStopped("discussion", f"已保存检查点“{checkpoint['title']}”（Working Version {checkpoint['working_version']}）。")

    def _finish_tool(self, arguments):
        self._args(arguments, ["reason", "summary", "remaining_issues", "evidence"])
        if not isinstance(arguments["reason"], str) or arguments["reason"] not in {"goal_reached", "no_progress", "needs_user_review", "capability_limit", "round_limit"}:
            raise ToolInputError("停止原因无效。")
        if arguments["reason"] == "round_limit" and self.state["used"]["edit_rounds"] < self.limits["max_rounds"]:
            raise ToolInputError("修改轮数尚未用完，不能报告达到修改轮数上限。")
        summary = self._text(arguments["summary"])
        issues = arguments["remaining_issues"]
        if not isinstance(issues, list) or len(issues) > 32 or any(not isinstance(i, str) or len(i) > 1000 for i in issues):
            raise ToolInputError("未解决事项格式无效。")
        selected = self.state["selected_revision"]
        if selected is None:
            if arguments["reason"] not in {"capability_limit", "needs_user_review"}:
                raise ToolInputError("尚未创建场景，不能宣称达到目标。")
            if arguments["evidence"] != []:
                raise ToolInputError("没有场景时不能声明截图证据。")
        else:
            self._evidence(arguments["evidence"], [selected], allow_geometry=True)
        if self.run.get('bootstrap'):
            readiness = self.manager.bootstrap_initial_readiness(self.identifier, selected, agent_state=self.state)
            if not readiness['ready'] and arguments['reason'] not in {'needs_user_review','capability_limit'}:
                raise ToolInputError('初版尚未满足发布条件。请依据当前候选主辅证据完成所缺步骤；若无法通过，可如实停止并保留未发布候选。',
                                     code='bootstrap_self_check_required', details=readiness)
        if self.state["pending_revision"] and arguments["reason"] != "needs_user_review":
            raise ToolInputError("仍有未比较的修改；先评审或交给用户判断。")
        if self.local_round_mode:
            document = self.manager.get_local_round(self.identifier)
            if document and document["status"] in {"active", "paused"}:
                raise ToolInputError("结束前需提交或明确结束当前局部草稿轮。")
        self.state["remaining_issues"] = issues
        self.state["model_summary"] = summary
        raise LoopStopped(arguments["reason"], summary)

    def _begin_local_round(self, arguments):
        self._args(arguments, ["goal", "criteria", "item_ids"])
        if self.state["pending_revision"]:
            raise ToolInputError("先评审待定候选。")
        return self.manager.begin_local_round(self.identifier, arguments, agent_state=self.state)

    def _checkpoint_local_round(self, arguments):
        self._args(arguments, ["reason"])
        reason = self._text(arguments["reason"])
        if self.state["pending_revision"]:
            raise ToolInputError("待定候选尚未评审，不能建立恢复点。")
        result = self.manager.checkpoint_local_round(self.identifier, agent_state=self.state,
            active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
        result["reason"] = reason
        self.state["_pause_after_tool"] = True
        return result

    def _commit_local_round(self, arguments):
        self._args(arguments, ["verdict", "preservation", "reason", "evidence"])
        if self.state["pending_revision"]:
            raise ToolInputError("先评审待定候选，再判断整轮。")
        if arguments["verdict"] != "better" or arguments["preservation"] != "pass":
            raise ToolInputError("整轮正式保存需要有证据的better和保留通过；其他结论请明确结束或待用户判断。")
        reason = self._text(arguments["reason"])
        document = self.manager.get_local_round(self.identifier)
        if not document or document["status"] != "active":
            raise RoundError("没有活跃局部轮。", "round_not_resumable", 409)
        start = document["start"]["revision_id"]
        selected = self.state["selected_revision"]
        if selected == start or selected != document["selected_revision"]:
            raise ToolInputError("本轮没有已确认的最终修改草稿。")
        self._verify_revision(self._revision(start))
        self._verify_revision(self._revision(selected))
        self._evidence(arguments["evidence"], [start, selected])
        paired = {}
        for observation in self.state["observations"]:
            if observation["id"] in arguments["evidence"] and observation["comparison"]:
                try:
                    key = (observation["view_id"],) + comparison_identity(observation)
                except ValueError:
                    continue
                paired.setdefault(key, set()).add(observation["revision_id"])
        if not any({start, selected}.issubset(revisions) for revisions in paired.values()):
            raise ToolInputError("整轮需引用起点和最终草稿在相同相机/渲染条件下的真实成对截图。")
        steps = [step for step in document["steps"] if step["status"] == "selected"]
        if not steps or steps[-1]["revision_id"] != selected:
            raise RoundError("最终草稿步骤历史不一致。", "round_operation_unknown", 409)
        parent = start
        operations = []
        for step in steps:
            if step["base_revision"] != parent:
                raise RoundError("草稿成功路径未连贯。", "round_operation_unknown", 409)
            parent = step["revision_id"]
            operations.extend(copy.deepcopy(step.get("operations", [])))
        committed = self.manager.commit_agent_revision(self.identifier, selected, reason, operations,
                                                       local_round_id=document["id"])
        self._revision(selected)["working_version"] = committed["workspace"]["working_version"]
        record = self.manager.get_local_round(self.identifier)
        for action in self.state["actions"]:
            if any(step["revision_id"] == action["revision_id"] for step in steps):
                action["status"] = "modified_pending_verification"
        self.state["last_round_commit"] = {"round_id": document["id"],
            "receipt_operation_id": record["receipt_operation_id"],
            "evidence": copy.deepcopy(arguments["evidence"]), "builder_self_check": reason}
        return {"round_id": document["id"], "receipt": record["commit_receipt"],
                "working_version": committed["workspace"]["working_version"],
                "builder_self_check": reason, "independent_judge": "separate"}

    def _end_local_round(self, arguments):
        self._args(arguments, ["reason"])
        if self.state["pending_revision"]:
            raise ToolInputError("待定候选需先评审或明确交用户处理。")
        document = self.manager.get_local_round(self.identifier)
        result = self.manager.end_local_round(self.identifier, arguments["reason"],
                                              from_builder=True)
        for action in self.state["actions"]:
            if any(step["revision_id"] == action["revision_id"] for step in document["steps"]):
                action["status"] = "not_committed"
        self.state["selected_revision"] = document["start"]["revision_id"]
        self._revision(self.state["selected_revision"])["status"] = "selected"
        return result

    def _judge_working_scene(self, arguments):
        self._args(arguments, ["reason"])
        self._text(arguments["reason"])
        document = self.manager.get_local_round(self.identifier)
        if document and document["status"] in {"active", "paused"}:
            raise RoundError("先提交或结束当前草稿轮，再评审正式Working。", "round_not_resumable", 409)
        self.state["_judge_after_tool"] = True
        return {"subject": "current_working", "requested": True}

    def _read_refinement(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {"item_ids"}:
            raise RefinementError("只支持可选item_ids读取范围。")
        return self.manager.get_refinement_plan(self.run["project_id"], item_ids=arguments.get("item_ids"))

    def _update_refinement(self, arguments):
        return self.manager.update_refinement_plan(self.identifier, arguments, agent_state=self.state)

    def _context(self):
        self._check_reference()
        chosen = {v for v in (self.state["selected_revision"], self.state["pending_revision"]) if v}
        local_document = self.manager.get_local_round(self.identifier) if self.local_round_mode else None
        if local_document and local_document["status"] in {"active", "paused"}:
            chosen.add(local_document["start"]["revision_id"])
        for identifier in chosen:
            self._verify_revision(self._revision(identifier))
        observations = [o for o in self.state["observations"] if o["revision_id"] in chosen and o["view_id"] in {"main", "auxiliary"}]
        diagnostics = [o for o in self.state["observations"] if o["revision_id"] in chosen and o["view_id"] not in {"main", "auxiliary"}]
        observations += diagnostics[-2:]
        self.seen = {o["id"] for o in observations} | {row["id"] for row in self.state.get("geometry", [])}
        self.seen_revisions = {o["revision_id"] for o in observations}
        model_limits = copy.deepcopy(self.limits)
        if self.local_round_mode and self.limits["schema_version"] == 2:
            model_limits.pop("max_rounds", None)
        model_limits.pop("max_seconds", None)
        if "completion_reserve" in model_limits:
            model_limits["completion_reserve"].pop("active_seconds", None)
        brief = {"selected_revision": self.state["selected_revision"], "pending_revision": self.state["pending_revision"],
                 "limits": model_limits, "used": {**self.state["used"], "brain_calls": self.manager.get(self.identifier)["usage"]["brain_calls"]},
                 "observations": [{k: o[k] for k in ("id", "revision_id", "view_id", "camera", "camera_id", "render_settings_id", "observation_mode", "observation_profile_id") if k in o} for o in observations]}
        if self.run.get('asset_capability'):
            brief['asset_request'] = self.manager.get_asset_request(self.identifier)
        if self.run.get('bootstrap') or self.local_round_mode:
            from .local_asset_permission import projection
            brief['local_asset_capability'] = projection(self.manager, self.manager.get(self.identifier))
        if self.local_round_mode and self.limits["schema_version"] == 2:
            brief["used_scope"] = {"edit_rounds": "run_draft_attempts"}
        for observation in brief["observations"]:
            if "camera" in observation:
                observation["camera"] = self._camera_summary(observation["camera"])
        brief["evaluation"] = self.run.get("evaluation", {"mode": "reference"})
        progress = (budget_progress(
            self.manager.get(self.identifier),
            self.manager._projects().state_store(self.run["project_id"]),
            round_document=local_document, agent_state=self.state,
            active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
            if self.local_round_mode else None)
        if self.run.get("goal_snapshot"):
            current_run = self.manager.get(self.identifier)
            plan = self.manager.get_refinement_plan(self.run["project_id"])
            if plan is None or plan["goal"]["id"] != self.run["goal_snapshot"]["id"]:
                raise RefinementError("当前目标已改变，停止旧执行上下文。", "refinement_goal_changed", 409)
            refinement = refinement_context(plan, current_run, self.state, budget=progress)
            if self.local_round_mode:
                if local_document and local_document["status"] in {"active", "paused"}:
                    refinement["candidate"]["kind"] = "local_round_draft"
                refinement["remaining"]["edit_attempts"] = progress["remaining"]["run_edit_attempts"]
                refinement["remaining"]["edit_attempts_scope"] = "run"
                refinement["remaining"]["edit_rounds_scope"] = "formal_saves"
                readiness = self.manager._projects().state_store(self.run["project_id"]).readiness(self.identifier)
                refinement["remaining"]["saved_edit_rounds"] = readiness["saved_edit_rounds"]
                refinement["remaining"]["saved_edit_rounds_scope"] = "used_run"
                refinement["remaining"]["target_saved_rounds"] = progress["limits"]["saved_round_limit"]
            from .refinement_evaluation import builder_feedback
            evaluation = builder_feedback(self.manager.get_refinement(self.run["project_id"])["evaluation"])
            refinement["unresolved_issues"] = evaluation.pop("unresolved_issues")
            refinement["evaluation_feedback"] = evaluation
            brief["refinement"] = refinement
        if self.local_round_mode:
            brief["budget"] = copy.deepcopy(progress)
            brief["budget"]["limits"].pop("max_seconds", None)
            brief["budget"]["limits"]["completion_reserve"].pop("active_seconds", None)
            brief["budget_accounting"] = "before_reserving_this_builder_call"
            brief["local_round"] = (None if local_document is None else {
                "round_id": local_document["id"], "round_version": local_document["version"],
                "status": local_document["status"], "goal_id": local_document["goal_id"],
                "goal_digest": local_document["goal_digest"],
                "local_goal": local_document["local_goal"],
                "local_criteria": local_document["local_criteria"],
                "criteria_items": local_document["criteria_items"],
                "start": local_document["start"],
                "selected_draft_revision": local_document["selected_revision"],
                "pending_revision": local_document["pending_revision"],
                "steps": [{key: copy.deepcopy(step.get(key)) for key in
                           ("id", "operation_id", "attempt_id", "retry_index", "revision_id",
                            "base_revision", "status", "assessment")} for step in local_document["steps"]],
                "checkpoint": copy.deepcopy(local_document["checkpoint"]),
                "commit_receipt": copy.deepcopy(local_document.get("commit_receipt")),
                "formal_working": copy.deepcopy(self.manager.get_project(self.run["project_id"])["current_scene"]),
                "remaining": {"edit_attempts": progress["remaining"]["round_edit_attempts"],
                              "edit_attempts_scope": "round",
                              "active_seconds": None},
                "elapsed_active_seconds": progress["used"]["active_seconds"],
                "judge_watermark": self.manager._projects().state_store(
                    self.run["project_id"]).readiness(self.identifier)["rounds_since_judge"],
                "meaning": "draft selection and Builder self-check; Working receipt and independent Judge remain separate"})
        selected = self.state["selected_revision"]
        evidence_revision = self.state["pending_revision"] or selected
        scene = self._build_evidence(self._revision(evidence_revision)) if evidence_revision else None
        brief["current_scene"] = builder_context.scene_index(scene) if scene else None
        if scene:
            brief["current_scene"]["projection"]["subject"] = "pending_candidate" if self.state["pending_revision"] else "current_selected"
        project = self.manager.get_project(self.run["project_id"]) if self.run.get("project_id") else None
        if project is not None:
            brief["working"] = {"selection": project["current_scene"], "working_version": project["workspace"]["working_version"]}
            from .judge_contract import scope_for_stage
            stage=project['workspace']['stage']
            brief['evaluation_stage']={'stage':stage,'scope':scope_for_stage(stage)[0]}
        if not self.run.get("goal_snapshot"):
            feedback = builder_context.legacy_feedback(self.state, project, self.identifier, selected)
            if project is not None:
                from .refinement_evaluation import builder_feedback
                projected = builder_feedback(self.manager.get_refinement(self.run["project_id"])["evaluation"])
                if projected["evaluations"]:
                    feedback = projected
            brief["unresolved_issues"] = feedback.pop("unresolved_issues")
            brief["evaluation_feedback"] = feedback
        self._trace("business_context", {"brief": brief, "scene": scene, "agent_state": self.state})
        from .observation_service import shared_baseline
        shared_refs,shared_images=shared_baseline(self.manager,self.run,self.adapter)
        if shared_refs:
            brief['shared_original_working_observations']=[{'ref':row['ref'],'identity':row['identity'],
                'source':self.run['base_scene'],'observation':row['observation']} for row in shared_refs]
        content = [{"type": "text", "text": "Current real observation evidence and comparison mode:\n" + json.dumps(brief, ensure_ascii=False)}]+shared_images
        if self.reference_path is not None:
            content += [{"type": "text", "text": "ADOPTED REFERENCE (fixed)"}, self.providers.image_content(self.reference_path)]
        for observation in observations:
            if observation.get('shared_ref'):
                cache=self._shared_cache(); record=cache.record(observation['cache_key'])
                if record['ref']!=observation['shared_ref'] or cache.source(self._workspace(self._revision(observation['revision_id'])),observation['view'])!=record['source']:
                    raise self.providers.ProviderError('observation_changed','共享图来源已变化，停止比较。')
                path=cache.resolve(record)
            else:
                path = self.root / observation["path"]
            if file_sha256(path) != observation["sha256"]:
                raise self.providers.ProviderError("observation_changed", "已登记的观察图片发生变化，停止比较。")
            content += [{"type": "text", "text": "OBSERVATION " + observation["id"] + " revision=" + observation["revision_id"] + " mode=" + observation.get("observation_mode", "appearance")
                        + " (structure is neutral inspection, not original material/light evidence)"},
                        self.providers.image_content(path)]
        return {"role": "user", "content": content}, brief

    def _initialize_existing(self):
        self._check_budget(observations=2)
        source = self.manager.scene_version(**self.run["base_scene"])
        revision = {"id": "r000", "parent_id": None, "round": 0, "status": "selected", "artifact_ids": [],
                    "source_scene": self.run["base_scene"]}
        self._event("building", "正在从项目当前场景创建本次修改的独立基线；不重新生图或重建原物体。")
        stage, generated = 'fork_candidate', None
        try:
            self.adapter.fork_candidate(source["workspace"], self._workspace(revision))
            stage, generated = 'verify_source', True
            self.manager.scene_version(**self.run["base_scene"])
            self._check_budget()
            stage = 'register_baseline'
            result = self._publish(revision)
        except Exception as error:
            from .local_diagnostics import annotate
            annotate(error, stage=stage, candidate_generated=generated)
            raise
        self.state["revisions"].append(revision)
        self.state["selected_revision"] = revision["id"]
        self._trace("existing_baseline", result)
        self._event("reviewing", "当前场景基线与双视角已就绪，大脑将从这里继续修改。", round=0)

    def _initialize_continuation(self, continuation):
        from .creative_continuation import verify
        host=self.manager._director_host()
        parent=self.manager.runs[self.run['director_delegation']['parent_id']]
        verify(host,parent,continuation,child_id=self.identifier)
        if continuation.get('kind')=='pending_edit':
            return self._initialize_pending_continuation(continuation,host,parent)
        self._check_budget(observations=2)
        source=continuation['source']
        version=self.manager.scene_version(source['run_id'],source['revision_id'])
        revision={'id':'r000','parent_id':None,'round':0,'status':'selected','artifact_ids':[],
                  'source_scene':copy.deepcopy(source)}
        self.manager._scene_operation(self.identifier,'r000','initial')
        self.adapter.fork_candidate(version['workspace'],self._workspace(revision))
        verify(host,parent,continuation,child_id=self.identifier)
        result=self._publish(revision)
        self.state['revisions'].append(revision);self.state['selected_revision']='r000'
        self.state['creative_continuation']={'identity':continuation['identity'],'source':copy.deepcopy(source),'target_revision':'r000'}
        self._trace('creative_baseline',result)
        self._event('reviewing','已从原任务最新已核候选复制后继基线；原版本与累计消耗保留，继续当前施工。',round=0)

    def _initialize_pending_continuation(self, continuation, host, parent):
        from .creative_continuation import verify, copy_plan
        self._check_budget(observations=4,edit=True)
        self._initialize_existing()
        source=continuation['source'];version=self.manager.scene_version(source['run_id'],source['revision_id'])
        original=continuation['pending_round']
        self.manager.begin_local_round(self.identifier,{'goal':original['local_goal'],
            'criteria':original['local_criteria'],'item_ids':original['item_ids']},agent_state=self.state)
        verify(host,parent,continuation,child_id=self.identifier)
        plan=copy_plan(continuation)
        operation=self.manager.start_local_step(self.identifier,'r001','r000',plan,candidate_copy=continuation['identity'])
        attempt_id=operation['attempt_id'];store=self.manager._projects().state_store(self.run['project_id'])
        revision={'id':'r001','parent_id':'r000','round':1,'status':'pending','artifact_ids':[],
            'step_id':attempt_id,'source_scene':copy.deepcopy(continuation['source'])}
        self.state['used']['edit_rounds']=store.local_edit_attempt_count(self.identifier)
        copied=False
        try:
            self.adapter.fork_candidate(version['workspace'],self._workspace(revision))
            copied=True
            if self.manager._fixed_scene_artifacts(self.manager.get(source['run_id']),source['revision_id'])!=continuation['fixed_artifacts']:
                raise self.providers.ProviderError('revision_changed','原草稿复制期间发生变化。')
            self.manager.scene_version(source['run_id'],source['revision_id'])
            self._check_budget()
            result=self._publish(revision)
            self.state['revisions'].append(revision);self.state['pending_revision']='r001'
            self.state['actions'].append({'revision_id':'r001',**copy.deepcopy(plan),'status':'pending'})
            self.state['creative_continuation']={'identity':continuation['identity'],'source':copy.deepcopy(source),'target_revision':'r001'}
        except Exception:
            outcome='failed' if copied else 'unknown'
            store.record_attempt_result(attempt_id,outcome=outcome,
                failure_category='integrity_failure' if copied else 'write_unknown',confirmed_no_commit=copied)
            self.manager.finish_local_step(self.identifier,attempt_id,status=outcome,detail='candidate_copy_evidence_failed' if copied else 'candidate_copy_unconfirmed')
            raise
        # A lost SQL acknowledgement remains scheduled/unconfirmed; never overwrite it with a second outcome.
        store.record_attempt_result(attempt_id,outcome='succeeded')
        self.manager.finish_local_step(self.identifier,attempt_id,status='pending',agent_state=self.state)
        self._trace('creative_pending_copy',result)
        self._event('reviewing','原草稿已复制并重新观察；未重放编辑，等待Builder比较真实Working基线并自检。',round=1)

    def _acceptance(self, trigger):
        self.state['_pending_judge'] = {**self.state.get('_pending_judge', {}),
                                        'mode': 'acceptance', 'trigger': trigger}
        result = self.manager.judge_agent_checkpoint(self.identifier, self.state, trigger)
        self.state.pop('_pending_judge', None)
        from .run_handoff import record_judge
        record_judge(self.state, result)
        if result["status"] == "not_requested":
            return result
        record = result.get("judgement") or {}
        identity = record.get("id")
        delivered = self.state.setdefault("judge_feedback_ids", [])
        if identity not in delivered:
            bound = (f"工作版本 {record.get('working_version')}" if record.get("working_version") is not None
                     else f"候选 {record.get('selection', {}).get('revision_id')}")
            feedback = {"status": result["status"], "trigger": result["trigger"],
                        "working_version": record.get("working_version"),
                        "selection": record.get("selection"), "verdict": record.get("verdict"),
                        "stage_passed": record.get("stage_passed"), "issues": record.get("issues", []),
                        "next_focus": record.get("next_focus", []), "unknowns": record.get("unknowns", []),
                        "message": record.get("message"),
                        "instruction": "This is independent acceptance of the stated committed version. "
                            "For a valid continue, inspect the cited problems and prioritize actual fixes within "
                            "remaining budgets; explain unresolved evidence honestly. Never treat failure as pass. "
                            "A rollback verdict is a recommendation, not an executed restore."}
            self.history.append({"role": "user", "content": "Independent Judge feedback:\n" +
                                 json.dumps(feedback, ensure_ascii=False)})
            self.state["builder_judge_feedback"] = copy.deepcopy(feedback)
            delivered.append(identity)
            self._trace("judge_feedback", feedback)
            self._event("agent_review", f"裁判反馈已交回 Builder（{bound}）。")
        return result

    @staticmethod
    def _not_executed_result():
        return {"ok": False, "error": {"code": "not_executed",
                "message": "未执行：已达到独立验收边界；收到裁判反馈后重新规划，未伪称执行。"}}

    def _pair_not_executed(self, call, *, message=None):
        call_id, name = call["id"], call["function"]["name"]
        fingerprint = hashlib.sha256((name + call["function"]["arguments"]).encode()).hexdigest()
        result = self._not_executed_result()
        if message:
            result["error"]["message"] = message
        if call_id in self.tool_results:
            previous_hash, result = self.tool_results[call_id]
            if previous_hash != fingerprint:
                raise self.providers.ProviderError("tool_id_conflict", "模型重复使用工具 ID 且参数不同，停止执行。")
        self.tool_results[call_id] = (fingerprint, result)
        self._trace("tool", {"id": call_id, "name": name, "result": result})
        self.history.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result, ensure_ascii=False)})

    def _edit_boundary_due(self):
        """True when newly committed rounds reached the periodic threshold."""
        if self.run.get("operation") != "edit":
            return False
        settings = self.run.get("judge") or {}
        if not settings.get("enabled"):
            return False
        if self.local_round_mode:
            readiness = self.manager._projects().state_store(self.run["project_id"]).readiness(self.identifier)
            committed = len(self.manager.get(self.identifier).get("commits", []))
            return (committed > int(self.state.get("_judged_commit_count", 0))
                    and readiness["rounds_since_judge"] is not None
                    and readiness["rounds_since_judge"] >= settings.get("trigger_rounds", 3))
        project = self.manager.get_project(self.run["project_id"])
        batch = project["workspace"].get("batch_start") or {}
        edits = int(batch.get("edit_rounds", 0))
        judged = int(batch.get("last_judged_round", 0))
        committed = len(self.manager.get(self.identifier).get("commits", []))
        return (committed > int(self.state.get("_judged_commit_count", 0))
                and edits - judged >= settings.get("trigger_rounds", 3))

    def _generate_boundary_due(self):
        """True when newly review-accepted candidates reached the threshold."""
        if self.run.get("operation") != "generate" or self.run.get('bootstrap'):
            return False
        settings = self.run.get("judge") or {}
        if not settings.get("enabled"):
            return False
        accepted = [row for row in self.state.get("actions", [])
                    if row.get("status") == "modified_pending_verification"]
        judged = max([row.get("accepted_count") or 0
                      for row in self.manager.get(self.identifier).get("judge_history", [])
                      if (row.get("key") or {}).get("kind") == "candidate"
                      and row.get("status") == "completed"] or [0])
        return len(accepted) - judged >= settings.get("trigger_rounds", 3)

    def _acceptance_boundary(self, calls, index):
        """Establish the acceptance boundary before any post-threshold write.

        Remaining calls of the current package are paired explicitly as
        not_executed (never silently run); Judge feedback reaches the Builder
        only with complete tool pairing, verified by the production validator.
        A pass completes the accepted scope and stops here: further edits need
        a new request with their own batch. Returns True when the package
        ended at this boundary.
        """
        due = self._edit_boundary_due() or self._generate_boundary_due()
        if not due:
            return False
        # Whether an acceptance is owed depends on accepted modifications and
        # the acceptance watermark, never on whether another edit round is
        # available: exhausting the edit budget must not skip the Judge.
        for call in calls[index + 1:]:
            self._pair_not_executed(call)
        acceptance = self._acceptance("periodic")
        _validate_history(self.history,builder_correction=self.correction)
        self.state["_judged_commit_count"] = len(self.manager.get(self.identifier).get("commits", []))
        record = acceptance.get("judgement") or {}
        if acceptance["status"] in {"failed", "stale"}:
            raise LoopStopped("needs_user_review", "独立裁判未能形成有效验收；保留已提交版本和错误证据。")
        if acceptance["status"] == "completed" and record.get("verdict") in {"uncertain", "rollback"}:
            raise LoopStopped("needs_user_review", "裁判存在不确定项或建议恢复；保留当前现场供确认。")
        if acceptance["status"] == "completed" and record.get("verdict") == "pass":
            bound = (f"工作版本 {record.get('working_version')}" if record.get("working_version") is not None
                     else f"候选 {record.get('selection', {}).get('revision_id')}")
            # A periodic pass completes the current acceptance scope only. It is
            # not a whole-request goal claim; any explicit follow-up scope needs
            # its own new request and batch.
            raise LoopStopped("stage_passed",
                              f"独立裁判已通过当前范围（{bound}），本轮停止；如需继续修改请发送新的要求。")
        if not self.run.get('agent_resource_base') and self._judge_budget_exhausted():
            raise LoopStopped("needs_user_review", "独立裁判调用额度已用完；后续修改已无法验收，本轮停止。")
        self._wait_for_judge_capacity(acceptance)
        return True

    def _wait_for_judge_capacity(self, acceptance):
        """A completed continue may need more quota before another draft starts."""
        run = self.manager.get(self.identifier)
        if (not run.get('agent_resource_base') or not self.local_round_mode
                or acceptance['status'] != 'completed'
                or (acceptance.get('judgement') or {}).get('verdict') != 'continue'
                or not self._judge_budget_exhausted() or run.get('cancel_requested')):
            return
        task_saves = next((row for row in run['resource_budget']['resources']
                           if row['resource'] == 'saved_rounds' and row['scope'] == 'task'), None)
        if task_saves and (task_saves['remaining'] is None or task_saves['remaining'] <= 0):
            return
        progress = budget_progress(run, self.manager._projects().state_store(run['project_id']),
                                  agent_state=self.state)
        # Ignore only the known empty Judge dimension. Other execution limits
        # and the formal-save target still prohibit starting another draft.
        available = copy.deepcopy(progress)
        available['remaining']['judge_calls'] = 1
        if edit_limit(available) is not None:
            return
        self.state['_pending_judge'] = {'mode': 'continue', 'trigger': acceptance['trigger'],
                                        'completed_input_id': acceptance['input_id']}
        from .judge_continuation import JudgeResourceWait
        raise JudgeResourceWait()

    def _pause_judge(self, error):
        if not self.run.get('agent_resource_base'):
            raise error
        if error.continuation:
            self.state['_pending_judge']['continuation'] = error.continuation
        self.manager._resource_host().exhausted(self, 'judge_calls')

    def _finalize(self, reason, summary):
        from .judge_continuation import JudgeResourceWait
        self.state['_pending_judge'] = {**self.state.get('_pending_judge', {}),
            'mode': 'finalize', 'trigger': reason, 'summary': summary}
        try:
            self.manager.finalize_agent_run(self.identifier, self.state, reason, summary)
            self.state.pop('_pending_judge', None)
        except JudgeResourceWait as error:
            self._pause_judge(error)

    def _judge_budget_exhausted(self):
        run = self.manager.get(self.identifier)
        settings = run.get("judge") or {}
        if not settings.get("enabled"):
            return False
        cap = (next(r['effective_limit'] for r in run['resource_budget']['resources'] if r['resource'] == 'judge_calls' and r['scope'] == 'run') if run.get('agent_resource_base') else
               resolve_snapshot(run)["max_judge_calls"] if self.local_round_mode else
               settings.get("max_calls", run.get("iteration", {}).get("max_rounds", 1)))
        return run["usage"].get("judge_calls", 0) >= cap

    def _can_continue(self):
        run = self.manager.get(self.identifier)
        if self.local_round_mode and self.limits["schema_version"] == 2:
            progress = budget_progress(
                run, self.manager._projects().state_store(run["project_id"]),
                agent_state=self.state,
                active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
            return edit_limit(progress) is None and not run.get("cancel_requested")
        return (self.state["used"]["edit_rounds"] < self.limits["max_rounds"]
                and run["usage"]["brain_calls"] < self.limits["max_brain_calls"]
                and self.state["used"]["tool_calls"] < self.limits["max_tool_calls"]
                and self.state["used"]["observations"] + 2 <= self.limits["max_observations"]
                and not run.get("cancel_requested"))

    def _guard_ledger(self):
        if self.manager.get(self.identifier).get('director_ledger_unknown'):
            raise self.providers.ProviderError('builder_ledger_unknown','调用账本未核实，停止后续请求和工具执行。',outcome_unknown=True)

    def _guard_correction(self, *, history=None):
        from .tool_correction import CorrectionCancelled
        try:
            if history is None:self.correction.guard()
            else:self.correction.check_request(history)
        except CorrectionCancelled as error:
            raise LoopStopped('cancelled',str(error)) from None

    def execute(self):
        from .judge_continuation import JudgeResourceWait
        self._event("agent_thinking", "视觉循环开始：大脑可建场、检查、截图、编辑并比较。")
        empty_responses = 0
        try:
            pending_judge = self.state.get('_pending_judge')
            if pending_judge and pending_judge['mode'] == 'continue':
                self.state.pop('_pending_judge')
                pending_judge = None
            if pending_judge:
                if pending_judge['mode'] == 'finalize':
                    self._finalize(pending_judge['trigger'], pending_judge['summary'])
                    return
                acceptance = self._acceptance(pending_judge['trigger'])
                self.state['_judged_commit_count'] = len(self.manager.get(self.identifier).get('commits', []))
                record = acceptance.get('judgement') or {}
                if acceptance['status'] != 'completed' or record.get('verdict') in {'uncertain', 'rollback'}:
                    raise LoopStopped('needs_user_review', '原评审未形成适用的继续结论，保留原候选与已提交版本。')
                if record.get('verdict') == 'pass':
                    reason = 'goal_reached' if pending_judge['trigger'] == 'goal_reached' else 'stage_passed'
                    raise LoopStopped(reason, '当前版本已通过原独立Judge。')
                self._wait_for_judge_capacity(acceptance)
            continuation=self.run.get('director_delegation',{}).get('brief',{}).get('creative_continuation')
            if continuation and not self.state['revisions']:
                for resource,minimum in (('builder_calls',1),('observations',4 if continuation.get('kind')=='pending_edit' else 2)):
                    row=next(r for r in self.run['resource_budget']['resources'] if r['resource']==resource and r['scope']=='run')
                    if row['remaining']<minimum:
                        self.manager._resource_host().exhausted(self,resource)
                        return
                self._initialize_continuation(continuation)
            if self.run.get("operation") == "edit" and not continuation and (not self.resume or self.state.get('_pending_initialization')):
                if self.run.get('agent_resource_base'):
                    self._check_budget()  # Cancel, baseline and current approved resource projection.
                    for resource, minimum in (('builder_calls', 1), ('observations', 2)):
                        row = next(r for r in self.run['resource_budget']['resources'] if r['resource'] == resource and r['scope'] == 'run')
                        if row['remaining'] < minimum:
                            if self.run.get('creative_resource_feedback', {}).get('status') == 'rejected':
                                self.state['stop_reason'] = 'capability_limit'
                                self._event('resource_rejected', '人类拒绝追加初始化所需资源；未创建候选或调用Builder，原Working保留。',
                                            status='completed', quality_status='needs_user_review')
                                return
                            self.state['_pending_initialization'] = {'kind': 'working_edit', 'base_scene': copy.deepcopy(self.run['base_scene'])}
                            self.manager._resource_host().exhausted(self, resource)
                            return
                self._initialize_existing()
                self.state.pop('_pending_initialization', None)
            if self.local_round_mode:
                readiness = self.manager._projects().state_store(self.run["project_id"]).readiness(self.identifier)
                since = readiness["rounds_since_judge"]
                if readiness["needs_reconciliation"] or since is None:
                    raise LoopStopped("needs_user_review", "正式保存/Judge计数待核对，不能写入草稿。")
                if since >= self.run["judge"]["trigger_rounds"]:
                    acceptance = self._acceptance("periodic")
                    record = acceptance.get("judgement") or {}
                    if acceptance["status"] != "completed":
                        raise LoopStopped("needs_user_review", "适用Judge尚未完成，不能写入新草稿。")
                    if record.get("verdict") == "pass":
                        raise LoopStopped("stage_passed", "当前Working已通过独立Judge。")
                    if record.get("verdict") != "continue":
                        raise LoopStopped("needs_user_review", "当前Working的Judge不允许继续自动编辑。")
                    if not self.run.get('agent_resource_base') and self._judge_budget_exhausted():
                        raise LoopStopped("needs_user_review", "Judge额度已用完，不能再启动需验收的草稿。")
                    self._wait_for_judge_capacity(acceptance)
            while True:
                self._check_budget()
                self._guard_ledger()
                if getattr(self, 'recovery', None):
                    from .tool_correction import CorrectionCancelled
                    try:
                        self.recovery.before_request()
                    except CorrectionCancelled:
                        raise LoopStopped('cancelled', '已取消原Builder的后续收束决策。')
                if self.correction:
                    self._guard_correction(history=self.history)
                    if self.correction.data['pending']:
                        for revision in {self.state['selected_revision'],self.state['pending_revision']}-{None}:
                            self._verify_revision(self._revision(revision))
                if self.manager.get(self.identifier)["usage"]["brain_calls"] >= self.limits["max_brain_calls"]:
                    if self.correction and self.correction.data['pending']:
                        self.correction.fail('tool_correction_budget_exhausted','原Builder调用额度不足，保留参数语法失败和全部纠错尝试。')
                    if self.run.get('agent_resource_base'):
                        self.manager._resource_host().exhausted(self, 'builder_calls')
                        return
                    if self.recovery.data['pending']:
                        self.recovery.fail('builder_length_budget_exhausted', '原Builder调用额度不足；保留截断诊断与恢复记录，交回Director。')
                    raise LoopStopped("brain_limit", "本轮大脑调用额度已用完，保留已有版本和未解决项。")
                if self.run.get('agent_resource_base') and self.state['used']['tool_calls'] >= self.limits['max_tool_calls']:
                    self.manager._resource_host().exhausted(self, 'tool_calls')
                    return
                context, brief = self._context()
                profile = self.manager._execution_profile(self.run["profiles"]["brain"])
                if not self.manager._reserve_call(self.identifier, "brain"):
                    raise LoopStopped("cancelled", "已停止后续模型调用。")
                if self.correction:self.correction.reserve_request()
                if self.recovery:self.recovery.reserve_request()
                if self.local_round_mode and brief.get("local_round"):
                    round_marker = {"round_id": brief["local_round"]["round_id"],
                                    "version": brief["local_round"]["round_version"],
                                    "selected_draft_revision": brief["local_round"]["selected_draft_revision"],
                                    "brain_call": self.manager.get(self.identifier)["usage"]["brain_calls"]}
                    self.state.setdefault("local_round_requests", []).append(round_marker)
                    self._trace("local_round_request", round_marker)
                if brief.get("refinement"):
                    value = brief["refinement"]
                    marker = {key: copy.deepcopy(value[key]) for key in
                              ("plan_id", "plan_version", "method_version", "method_binding", "focus_item_id", "current_scene", "candidate", "remaining")}
                    marker.update(goal_id=value["goal"]["id"], goal_digest=self.manager._digest(value["goal"]),
                                  brain_call=self.manager.get(self.identifier)["usage"]["brain_calls"])
                    self.state.setdefault("refinement_requests", []).append(marker)
                    self._trace("refinement_request", marker)
                self._event("agent_thinking", f"大脑正在观察与决策（最多 {self.limits['max_brain_calls']} 次调用）。")
                request_tools = self._available_tools()
                messages, metrics = builder_context.assemble(self.system, self.history, context, brief, self.run, self.state,
                                                             audit=self.audit)
                self._trace("builder_request_shape", metrics)
                self.history = messages
                try:
                    assistant = self.manager._agent_step(self.identifier, profile, messages, request_tools,
                                                         role="builder", timeout=brain_timeout(profile))
                    self._trace("assistant", {k: v for k, v in assistant.items() if k != "reasoning_content"})
                    self._guard_ledger()
                    self.recovery.response()
                    if self.correction and self.correction.data['pending']:self._guard_correction()
                except Exception as error:
                    if self.correction:self.correction.request_failed(error)
                    self.recovery.response(error)
                    from .providers import OutputLengthExhausted
                    if isinstance(error, OutputLengthExhausted):
                        try:
                            self.recovery.length(error)
                        except CorrectionCancelled:
                            raise LoopStopped('cancelled', '已取消原Builder的后续收束决策。')
                        self._event('agent_tool_feedback', '单次思考触及输出上限，原Builder将依据已确认状态收束下一项操作；没有执行截断输出。')
                        continue
                    raise
                # Model latency must not turn externally changed goals/files into accepted evidence.
                self.manager._assert_refinement_goal(self.run)
                self._check_reference()
                for identifier in {self.state["selected_revision"], self.state["pending_revision"]} - {None}:
                    self._verify_revision(self._revision(identifier))
                replies=self.correction.accept(assistant) if self.correction else []
                self.history.append(assistant)
                if replies:
                    self.history.extend(replies)
                    for call,reply in zip(assistant['tool_calls'],replies):
                        self._trace('tool',{'id':call['id'],'name':call['function']['name'],'result':json.loads(reply['content'])})
                    _validate_history(self.history,builder_correction=self.correction)
                    self._event('agent_tool_feedback','工具参数JSON无效，整批确认未执行；已将完整配对反馈给原Builder。')
                    if self.manager._cancelled(self.identifier):
                        raise LoopStopped('cancelled','已取消后续纠错，原未执行回执保留。')
                    self.correction.paired()
                    continue
                calls = assistant.get("tool_calls") or []
                if not calls:
                    empty_responses += 1
                    if empty_responses >= 2:
                        raise LoopStopped("needs_user_review", "大脑没有继续提供可执行工具调用，保留已有结果供检查。")
                    self.history.append({"role": "user", "content": "Use the tools to act or finish with evidence. Text alone cannot create/edit or accept a scene."})
                    continue
                empty_responses = 0
                for position, call in enumerate(calls):
                    self._guard_ledger()
                    name = call["function"]["name"]
                    if name == 'save_checkpoint' and self.run.get('bootstrap'):
                        fingerprint = hashlib.sha256((name + call['function']['arguments']).encode()).hexdigest()
                        result = {'ok': False, 'error': {'code': 'tool_validation_error', 'message': '初建没有Working检查点；请使用checkpoint_candidate。'}}
                        self.tool_results[call['id']] = (fingerprint, result)
                        self._trace('tool', {'id': call['id'], 'name': name, 'result': result})
                        self.history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)})
                        continue
                    if name not in {tool["function"]["name"] for tool in self._available_tools()}:
                        self._pair_not_executed(
                            call, message="未执行：Run 已进入收尾，剩余额度仅供当前合法评审、正式保存或安全结束。")
                        continue
                    if self.run.get('agent_resource_base') and self.state['used']['tool_calls'] >= self.limits['max_tool_calls']:
                        for remaining in calls[position:]:
                            self._pair_not_executed(remaining, message='工具额度耗尽；未执行，等待资源批准。')
                        _validate_history(self.history,builder_correction=self.correction)
                        self.manager._resource_host().exhausted(self, 'tool_calls')
                        return
                    self._check_budget(tool=True)
                    self.state["used"]["tool_calls"] += 1
                    call_id = call["id"]
                    raw = call["function"]["arguments"]
                    fingerprint = hashlib.sha256((name + raw).encode()).hexdigest()
                    reused_receipt = call_id in self.tool_results
                    if call_id in self.tool_results:
                        previous_hash, result = self.tool_results[call_id]
                        if previous_hash != fingerprint:
                            raise self.providers.ProviderError("tool_id_conflict", "模型重复使用工具 ID 且参数不同，停止执行。")
                    else:
                        self._event("agent_tool", f"正在执行工具 {name}。")
                        try:
                            dispatch = {"read_refinement_plan": self._read_refinement,
                                        "update_refinement_plan": self._update_refinement,
                                        "begin_local_round": self._begin_local_round,
                                        "checkpoint_local_round": self._checkpoint_local_round,
                                        "commit_local_round": self._commit_local_round,
                                        "end_local_round": self._end_local_round,
                                        "judge_working_scene": self._judge_working_scene,
                                        "create_scene": self._create, "inspect_scene": self._inspect, "observe_scene": self._observe,
                                        "review_initial":self._review_initial,
                                        "inspect_geometry": self._geometry, "apply_edit": self._edit,
                                        "review_candidate": self._review, "respond": self._respond,
                                        "save_checkpoint": self._checkpoint, "finish": self._finish_tool}
                            from . import builder_memory
                            dispatch['manage_builder_context'] = lambda args: builder_memory.manage(self, args)
                            if self.run.get('asset_capability'):
                                from . import asset_builder
                                dispatch.update(request_asset=lambda args:asset_builder.request(self,args),
                                    read_asset_request=lambda args:asset_builder.read(self,args),
                                    import_asset=lambda args:asset_builder.import_asset(self,args))
                                if hasattr(asset_builder,'transform_asset'):
                                    dispatch['transform_asset']=lambda args:asset_builder.transform_asset(self,args)
                            if self.run.get('agent_resource_base'):
                                resource_host = self.manager._resource_host()
                                dispatch.update(checkpoint_candidate=lambda args: resource_host.checkpoint_tool(self, args),
                                                request_resources=lambda args: resource_host.request_tool(self, args))
                            if name not in dispatch:
                                raise ToolInputError("工具不存在。")
                            result = {"ok": True, "result": dispatch[name](json.loads(raw))}
                        except LoopStopped as stop:
                            # Pair the completion request and every remaining call
                            # before independently judging it: Judge feedback must
                            # never split an unfinished tool pairing, and a valid
                            # continue resumes this same Run for replanning.
                            terminal_finish = (name == 'finish' and self.run.get('agent_resource_base')
                                and (stop.reason != 'goal_reached' or not self._can_continue()))
                            if name == 'finish' and (self.run.get('bootstrap') or terminal_finish):
                                result = {'ok':True, 'result':{'finished':True, 'reason':stop.reason,
                                    'candidate_revision':self.state['selected_revision'], 'publication':'pending_finalization'}}
                                if not self.run.get('bootstrap'):
                                    result = {'ok': True, 'result': {'completion_requested': True,
                                        'reason': stop.reason, 'independent_acceptance': 'pending_finalization'}}
                                self.tool_results[call_id] = (fingerprint, result)
                                self._trace('tool', {'id':call_id, 'name':name, 'result':result})
                                self.history.append({'role':'tool','tool_call_id':call_id,'content':json.dumps(result,ensure_ascii=False)})
                                for remaining in calls[position + 1:]:
                                    self._pair_not_executed(remaining,message='未执行：Builder已明确结束本次施工，后续调用不再运行。')
                                _validate_history(self.history,builder_correction=self.correction)
                                self._event('agent_tool', 'Builder已提交收尾决定；按实际自检与发布条件保留结果。')
                                raise
                            if name != "finish" or stop.reason != "goal_reached" or not self._can_continue():
                                raise
                            self.history.append({"role": "tool", "tool_call_id": call_id,
                                                 "content": json.dumps({"completion_requested": True})})
                            self.tool_results[call_id] = (fingerprint, {'ok': True,
                                'result': {'completion_requested': True, 'independent_acceptance': 'pending'}})
                            for remaining in calls[position + 1:]:
                                self._pair_not_executed(remaining)
                            acceptance = self._acceptance("goal_reached")
                            _validate_history(self.history,builder_correction=self.correction)
                            record = acceptance.get("judgement") or {}
                            if acceptance["status"] != "completed" or record.get("verdict") != "continue":
                                raise
                            if not self.run.get('agent_resource_base') and self._judge_budget_exhausted():
                                raise LoopStopped("needs_user_review", "独立裁判调用额度已用完；后续修改已无法验收，本轮停止。")
                            self._wait_for_judge_capacity(acceptance)
                            result = {"ok": True, "result": {"finished": False,
                                      "reason": "Independent Judge requests further work", "judgement": record}}
                            self.tool_results[call_id] = (fingerprint, result)
                            self._trace("tool", {"id": call_id, "name": name, "result": result})
                            # Only the placeholder above is paired in history (one
                            # tool result per call); details stay in tool_results.
                            self._event("agent_tool", "大脑申请完成；独立裁判要求继续施工，已交回反馈重新规划。")
                            break
                        except UnexecutedToolRejected as error:
                            result = rejection_receipt(error, call_id, name)
                        except ToolResultError as error:
                            result = {'ok':False,'error':{'code':error.code,'message':str(error)}}
                            if error.details is not None:result['error']['details']=copy.deepcopy(error.details)
                        except (RefinementError, RoundError) as error:
                            result = {"ok": False, "error": {"code": error.code, "message": str(error)}}
                            if error.current_version is not None:
                                result["error"]["current_version"] = error.current_version
                        except AssetJobError as error:
                            result = {"ok":False,"error":{"code":error.code,"message":str(error)}}
                            if error.details is not None:
                                result['error']['details'] = copy.deepcopy(error.details)
                            if name == 'import_asset':
                                doc = self.manager._asset_host().store(self.manager,self.identifier).request(self.identifier)
                                if doc and doc['import_intent'] and not doc['import_receipt']:
                                    self.state['_asset_import_stopped'] = True
                        self.tool_results[call_id] = (fingerprint, result)
                    self._trace("tool", {"id": call_id, "name": name, "result": result, "receipt_reused": reused_receipt})
                    # The geometry document must reach the provider in exactly the form that
                    # document_bytes measured; other tool results keep the default format.
                    content = (compact_json(result) if name == "inspect_geometry"
                               else json.dumps(result, ensure_ascii=False))
                    self.history.append({"role": "tool", "tool_call_id": call_id, "content": content})
                    self.recovery.progress(name, result, reused=reused_receipt)
                    self._event("agent_tool", f"工具 {name} 已返回{'结果' if result['ok'] else '需要修正的参数'}。")
                    if (not result['ok'] and result.get('error', {}).get('details', {}).get('execution') == 'confirmed_not_executed'):
                        for remaining in calls[position + 1:]:
                            self._pair_not_executed(remaining, message='未执行：前序调用被可信写前校验拒绝；保留之前成功结果，依据回执重新规划本批依赖工具。')
                        _validate_history(self.history,builder_correction=self.correction)
                        break
                    if result['ok'] and self.resource_boundary:
                        for remaining in calls[position + 1:]:
                            self._pair_not_executed(remaining, message='未执行：候选/native检查点边界；继续后重新规划。')
                        _validate_history(self.history,builder_correction=self.correction)
                        if self.manager._resource_host().seal_boundary(self): return
                        break
                    if result['ok'] and self.asset_pause:
                        for remaining in calls[position + 1:]:
                            self._pair_not_executed(remaining,message='未执行：资产请求等待人类批准；恢复后重新规划。')
                        _validate_history(self.history,builder_correction=self.correction)
                        from .asset_builder import pause
                        pause(self)
                        return
                    if self.state.pop('_asset_import_stopped',False):
                        for remaining in calls[position+1:]:
                            self._pair_not_executed(remaining,message='未执行：资产导入已有不可重放结果，等待核对。')
                        _validate_history(self.history,builder_correction=self.correction)
                        raise LoopStopped('needs_user_review','资产导入结果保留，不能自动重导入。')
                    if result["ok"] and self.state.pop("_pause_after_tool", False):
                        for remaining in calls[position + 1:]:
                            self._pair_not_executed(remaining)
                        _validate_history(self.history,builder_correction=self.correction)
                        raise LoopStopped("draft_checkpoint", "草稿恢复点已固定，等待显式续做。")
                    if result["ok"] and self.state.pop("_judge_after_tool", False):
                        for remaining in calls[position + 1:]:
                            self._pair_not_executed(remaining)
                        acceptance = self._acceptance("goal_reached")
                        _validate_history(self.history,builder_correction=self.correction)
                        record = acceptance.get("judgement") or {}
                        if acceptance["status"] != "completed" or record.get("verdict") in {"uncertain", "rollback"}:
                            raise LoopStopped("needs_user_review", "当前Working的独立Judge未形成适用继续结论。")
                        if record.get("verdict") == "pass":
                            raise LoopStopped("stage_passed", "当前Working已通过独立Judge。")
                        self._wait_for_judge_capacity(acceptance)
                        break
                    # Acceptance boundary lives here, right after each paired
                    # result: a committed round that reaches the threshold is
                    # judged before any later write in the same package. Pure
                    # inspection and rejected candidates never cross it.
                    if self._acceptance_boundary(calls, position):
                        break
        except JudgeResourceWait as error:
            self._pause_judge(error)
            return
        except LoopStopped as stop:
            if self.correction and self.correction.data['batches']:
                if stop.reason!='cancelled' and (self.correction.data['pending'] or not self.state['revisions']):
                    self.correction.stopped(stop.reason)
            self.state["stop_reason"] = stop.reason
            if stop.reason == "draft_checkpoint" and self.local_round_mode:
                return
            if self.local_round_mode:
                document = self.manager.get_local_round(self.identifier)
                if document and document["status"] in {"active", "paused"}:
                    if document["status"] == "active":
                        self.manager.suspend_local_round(
                            self.identifier, agent_state=self.state,
                            active_seconds=self.active_seconds_prior + time.monotonic() - self.started)
                    if stop.reason == "cancelled":
                        self._event("cancelled", stop.summary, status="cancelled")
                    else:
                        self._event("interrupted", stop.summary, status="interrupted",
                                    quality_status="needs_user_review",
                                    error={"code": "round_paused_without_checkpoint",
                                           "message": stop.summary, "outcome_unknown": False})
                    return
            if stop.reason == "cancelled":
                self._event("cancelled", stop.summary, status="cancelled")
                return
            selected = self.state["selected_revision"]
            if selected is None:
                self._event("failed", stop.summary, status="failed", quality_status="not_evaluated",
                            error={"code": "agent_no_scene", "message": stop.summary, "outcome_unknown": False})
            else:
                self._finalize(stop.reason, stop.summary)
