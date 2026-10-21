"""Deterministic Builder request views and immutable, reasoning-free business audit.

Views never replace the execution ledger, scene artifacts, Plan or round state.
The most recent requested geometry document is deliberately not byte-truncated.
"""
import copy
import hashlib
import json
from pathlib import Path
import re

RECENT_DECISIONS = 8
FOCUSED_EVIDENCE = 4
ANCHOR = "Continue the user's task using the current state, constraints and evidence in the final message. Earlier exchanges are omitted, not undone."
_PRIVATE_KEYS = {"reasoning_content", "reasoning", "thinking", "api_key", "apikey", "authorization",
                 "access_token", "refresh_token", "credential", "secret", "password"}
_IMAGE = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fields(value, names):
    return {key: copy.deepcopy(value[key]) for key in names if key in value}


def operations(rows):
    """Identify changes without repeating authored mesh arrays or entire objects."""
    result = []
    for row in rows:
        item = fields(row, ("op", "id", "part_id", "mode"))
        if row.get('instance_id'):item['instance_id']=row['instance_id']
        if row.get('target'):item['target']=copy.deepcopy(row['target'])
        if row.get('op')=='assign_surface':item['surface']=copy.deepcopy(row.get('surface'))
        if row.get('op')=='configure_lighting':item['lighting']=copy.deepcopy(row.get('lighting'))
        if isinstance(row.get("object"), dict):
            item["id"] = row["object"].get("id")
        if isinstance(row.get("part"), dict):
            item["part_id"] = row["part"].get("id")
        if isinstance(row.get("changes"), dict):
            item["changed_fields"] = sorted(row["changes"])
        result.append(item)
    return result


def scene_index(evidence):
    """Current object-level facts; explicit focused inspections remain untouched."""
    result = copy.deepcopy(evidence)
    for row in result.get("declared", {}).get("objects", []):
        for key in ("part_labels", "material_labels"):
            row.pop(key, None)
    for row in result.get("measured", {}).get("objects", []):
        row.get("measured", {}).pop("parts", None)
    specification = result.get("editable_specification", {})
    specification["objects"] = [fields(row, (
        "id", "name", "kind", "key_object", "position", "rotation", "scale", "dimensions",
        "editable_operations", "editable_fields", "readonly_reasons", "planned_part_count",
        "planned_details_available", "managed_template")) for row in specification.get("objects", [])]
    specification["scene"] = fields(specification.get("scene", {}), (
        "scene_name", "scene_id", "frame", "unit_system", "unit_scale_length"))
    derived = result.get("derived_geometry", {})
    if "parts" in derived:
        derived["parts"] = [fields(row, ("object_id", "part_id", "status", "current")) for row in derived["parts"]]
    result["projection"] = {"kind": "builder_object_index", "detail_omitted": True,
                            "read_details": "inspect_scene(revision_id, object_ids) for complete editable parameters and measured parts; inspect_geometry for actual mesh arrays."}
    return result


def tool_view(name, result):
    if not result.get("ok") or name not in {"apply_edit", "create_scene", "import_asset"}:
        return copy.deepcopy(result)
    source = result.get("result", {})
    revision = source.get("revision", {})
    receipt = {"revision": fields(revision, ("id", "parent_id", "round", "status", "step_id", "working_version", "edit_reason")),
               "changes": operations(revision.get("operations", [])),
               "evidence_source": copy.deepcopy(source.get("inspection", {}).get("source")),
               "observations": [fields(row, ("id", "revision_id", "view_id", "camera_id", "render_settings_id", "observation_mode", "observation_profile_id"))
                                for row in source.get("observations", [])],
               "detail_omitted": True,
               "detail_access": "Current object index is in the final context; call inspect_scene with object_ids for full target details."}
    if "cut_measurements" in source:
        receipt["cut_measurements"] = copy.deepcopy(source["cut_measurements"])
    if name == 'import_asset':
        receipt['asset'] = fields(source.get('asset',{}),('instance_id','source','transform'))
        receipt.update(draft_only=True,working_unchanged=True)
    return {"ok": True, "result": receipt}


def latest_exchange(history):
    """Keep one complete native batch (or a plain assistant answer), never half a batch."""
    for index in range(len(history) - 1, -1, -1):
        assistant = history[index]
        if assistant.get("role") != "assistant":
            continue
        calls = assistant.get("tool_calls") or []
        expected = {call["id"]: call["function"]["name"] for call in calls}
        paired = [row for row in history[index + 1:] if row.get("role") == "tool"]
        if len(paired) != len(expected) or {row.get("tool_call_id") for row in paired} != set(expected):
            raise ValueError("Builder native exchange is incomplete")
        group = [copy.deepcopy(assistant)]
        for row in paired:
            item = copy.deepcopy(row)
            name = expected[row["tool_call_id"]]
            if name in {"apply_edit", "create_scene", "import_asset"}:
                item["content"] = encoded(tool_view(name, json.loads(row["content"])))
            # Other strings, especially geometry document_bytes, are byte-for-byte intact.
            group.append(item)
        return group
    return []


def record_decision(state, value, audit):
    from .run_handoff import record_tool
    record_tool(state, value)
    result = value["result"]
    if value.get("receipt_reused"):
        return
    if value["name"] in {"inspect_scene", "inspect_geometry", "observe_scene", "read_refinement_plan", "manage_builder_context"} and result.get("ok"):
        state["builder_read_count"] = state.get("builder_read_count", 0) + 1
        return
    body = result.get("result") or {}
    row = {"call_id": value["id"], "tool": value["name"], "ok": result.get("ok"),
           "audit": copy.deepcopy(audit)}
    if not result.get("ok"):
        row["error"] = fields(result.get("error", {}), ("code", "message"))
    elif isinstance(body, dict):
        row.update(fields(body, ("revision_id", "selected_revision", "round_id", "working_version", "draft_only", "working_unchanged")))
        revision = body.get("revision") or {}
        if isinstance(revision, dict) and revision:
            row["revision"] = fields(revision, ("id", "parent_id", "round", "status", "step_id"))
            row["changes"] = operations(revision.get("operations", []))
        if isinstance(body.get("assessment"), dict):
            row["builder_assessment"] = fields(body["assessment"], ("verdict", "preservation", "reason", "evidence"))
    rows = state.setdefault("builder_decisions", [])
    rows.append(row)
    del rows[:-RECENT_DECISIONS]
    state["builder_decision_count"] = state.get("builder_decision_count", 0) + 1


def decision_view(state):
    from .builder_memory import omitted
    rows = copy.deepcopy(state.get("builder_decisions", []))
    count = state.get("builder_decision_count", len(rows))
    if not rows:
        actions = state.get("actions", [])
        rows = [{**fields(row, ("revision_id", "status", "reason")),
                 "changes": operations(row.get("operations", [])), "source": "persisted_agent_actions"}
                for row in actions[-RECENT_DECISIONS:]]
        count = len(actions)
    archived = omitted(state)
    rows = [r for r in rows if (r.get('audit') or {}).get('sequence') not in archived]
    return {"recent": rows[-RECENT_DECISIONS:], "omitted_count": max(0, count - len(rows)),
            "read_calls": state.get("builder_read_count", 0),
            "audit_source": "manage_builder_context list/read retrieves current Run registered immutable business audit",
            "meaning": "Execution receipts and Builder self-checks; independent Judge conclusions are separate."}


def remember_evidence(state, value, reference):
    if value["name"] not in {"inspect_scene", "inspect_geometry"} or not value["result"].get("ok"):
        return
    body = value["result"]["result"]
    if value["name"] == "inspect_scene":
        document = body.get("inspection", {})
        focus = document.get("focus", {}).get("object_ids")
        if not focus:
            return
        identity = {"revision_id": body["revision_id"], "object_ids": sorted(focus)}
    else:
        identity = fields(body["source"], ("revision_id", "object_id", "part_id", "representation"))
    identity["kind"] = value["name"]
    rows = [row for row in state.get("builder_evidence", []) if row["identity"] != identity]
    rows.append({"identity": identity, "call_id": value["id"], "audit": copy.deepcopy(reference)})
    state["builder_evidence"] = rows[-FOCUSED_EVIDENCE:]


def evidence_view(state, brief, group, audit):
    from .builder_memory import omitted
    recent_ids = {row["tool_call_id"] for row in group if row["role"] == "tool"}
    selected, pending = brief.get("selected_revision"), brief.get("pending_revision")
    local = brief.get("local_round") or {}
    start = (local.get("start") or {}).get("revision_id") if local.get("status") in {"active", "paused"} else None
    result = {"entries": [], "invalidated_count": 0, "capacity": FOCUSED_EVIDENCE,
              "cache_status": "available" if "builder_evidence" in state else "not_cached",
              "note": "Frozen requested details only; omitted/unavailable details require inspect_scene or inspect_geometry again."}
    for row in state.get("builder_evidence", [])[-FOCUSED_EVIDENCE:]:
        if (row.get('audit') or {}).get('sequence') in omitted(state) and row['call_id'] not in recent_ids:
            continue
        revision = row["identity"].get("revision_id")
        if revision not in {selected, pending, start} - {None}:
            result["invalidated_count"] += 1
            continue
        item = copy.deepcopy(row)
        item["scope"] = ("pending_candidate" if revision == pending else "selected_before_pending" if pending and revision == selected
                         else "current_selected" if revision == selected else "round_start_before")
        if row["call_id"] in recent_ids:
            item["content_location"] = "latest_native_exchange"
        else:
            try:
                original = audit.read(row["audit"])
                if original["kind"] != "tool" or original["value"]["id"] != row["call_id"]:
                    raise ValueError("audit identity mismatch")
                item["result"] = original["value"]["result"]
                item["content_location"] = "verified_immutable_audit"
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                item["content_location"] = "unavailable_reinspect_required"
        result["entries"].append(item)
    return result


def legacy_feedback(state, project, identifier, selected):
    workspace = (project or {}).get("workspace", {})
    latest = state.get("builder_judge_feedback") or workspace.get("latest_judgement") or {}
    target = latest.get("selection") or {}
    if latest.get("working_version") is None:
        current = bool(latest) and target == {"run_id": identifier, "revision_id": selected}
    else:
        current = target == (project or {}).get("current_scene") and latest["working_version"] == workspace.get("working_version")
    feedback = fields(latest, ("status", "trigger", "working_version", "selection", "verdict", "stage_passed",
                               "next_focus", "unknowns", "message", "instruction"))
    feedback["current"] = bool(current)
    feedback["applicability"] = "current_version" if current else "historical_or_unavailable"
    issues = {row["id"]: {key: copy.deepcopy(value) for key, value in row.items() if key not in {"reviews", "review_history"}}
              for row in workspace.get("issues", []) if row.get("status") != "verified" or row.get("working_version") != workspace.get("working_version")}
    if current:
        for row in latest.get("issues", []):
            issues.setdefault(row["id"], copy.deepcopy(row))
    return {"latest_acceptance": feedback, "unresolved_issues": list(issues.values())}


def assemble(system, history, context, brief, run, state, *, audit=None):
    if run.get('director_delegation'):
        delegated = run['director_delegation']
        scope = delegated['brief']
        frozen = {key:copy.deepcopy(scope[key]) for key in ('goal','criteria','object_ids','item_ids','preserve','goal_ref')}
        frozen.update(task_id=delegated['task_id'],delegation_id=delegated['delegation_id'],
            parent_run_id=delegated['parent_id'],baseline={key:scope['baseline'][key] for key in
                ('selection','working_version','blend_sha256','plan_sha256') if key in scope['baseline']})
        if scope.get('creation_authorization'):
            from .director_creation import public_authority
            frozen['creative_authorization']=public_authority(scope['creation_authorization'])
            frozen['bootstrap']=scope.get('bootstrap') is True
        if scope.get('plan_authorization'): frozen['plan_authorization']=copy.deepcopy(scope['plan_authorization'])
        system += '\nFrozen local delegation (global Goal unchanged; choose your own parameters/tools):\n' + encoded(frozen)
        system += ('\nPlan updates in this delegation cannot add global items or set order. Frozen title, desired, basis, '
                   'criteria and object_ids (including an empty scope) stay unchanged. Use method, next_step and '
                   'self_check on authorized items to plan execution; focus only an authorized item. A rejected Plan '
                   'update leaves later calls in that batch unexecuted; read the paired results and choose new calls.')
        if scope.get('creative_continuation'):
            system += '\nSuccessor of verified unpublished construction: the Harness forked the selected source into this Run r000. Continue that geometry/assets/materials; create_scene is unavailable. Inspect current fixed images, perform actual edits, review_initial and finish for the existing initial SQL publication gate. Original model notes are opinions, never permissions or confirmed results.'
        elif scope.get('bootstrap'):
            system += '\nEmpty baseline: create_scene, inspect actual fixed main/auxiliary images, then review_initial with pass before finishing. Initial Working is a separate initial receipt, never an edit-round save. No concept-image generation is required.'
    view = copy.deepcopy(brief)
    view["request"] = run["prompt"]
    view["constraints"] = copy.deepcopy(run["constraints"])
    if run.get('resource_budget'):
        view['resource_budget'] = copy.deepcopy(run['resource_budget'])
        view['candidate_checkpoint'] = copy.deepcopy(state.get('candidate_checkpoint'))
    view["design_context"] = copy.deepcopy(run.get("design_context"))
    view["decisions"] = decision_view(state)
    from . import builder_memory, builder_recovery
    view['builder_working_memory'] = builder_memory.view(state)
    continuation=run.get('director_delegation',{}).get('brief',{}).get('creative_continuation')
    if continuation:
        from .creative_continuation import public
        inherited=public(continuation)
        memory=inherited.pop('memory',None)
        view['creative_continuation']=inherited
        view['inherited_builder_opinions']={'source':'model_authored_unverified','source_run_id':continuation['source']['run_id'],
            'memory':memory,'meaning':'Historical model opinions only; current artifacts, permissions and ledgers remain authoritative. No native calls or reasoning were reconstructed.'}
    if run.get('creative_resource_feedback'):
        view['resource_request']=copy.deepcopy(run['creative_resource_feedback'])
    recovery = builder_recovery.context(state)
    if recovery:
        view['output_recovery'] = recovery
    view["audit_status"] = copy.deepcopy(state.get("builder_audit", {"status": "complete"}))
    correction=run.get('builder_tool_correction')
    if correction:
        view['tool_argument_correction']={'used':correction['correction_calls'],'limit':2,
            'pending':bool(correction['pending']),
            'instruction':'JSON语法错误批次全部未执行；保留原native配对，重新规划完整参数。纠错仍消耗本Run原调用额度，不能重用旧ID。'}
    group = latest_exchange(history)
    view["focused_evidence"] = evidence_view(state, brief, group, audit)
    last = copy.deepcopy(context)
    last["content"][0]["text"] = "Current real observation evidence and comparison mode:\n" + encoded(view)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ANCHOR}] + group + [last]
    from .run_handoff import incoming_text
    historical = incoming_text(run)
    if historical is not None:
        messages.insert(2, {"role": "user", "content": historical})
    text_messages = copy.deepcopy(messages)
    for row in text_messages:
        if isinstance(row.get("content"), list):
            row["content"] = [part for part in row["content"] if part.get("type") != "image_url"]
    metrics = {"text_bytes": len(encoded(text_messages).encode("utf-8")),
        "system_bytes": len(system.encode("utf-8")), "current_state_bytes": len(encoded(view).encode("utf-8")),
        "native_group_bytes": len(encoded(group).encode("utf-8")),
        "native_reasoning_bytes": sum(len((row.get("reasoning_content") or "").encode("utf-8")) for row in group),
        "message_count": len(messages), "image_count": sum(part.get("type") == "image_url" for part in last["content"]),
        "meaning": "UTF-8 bytes, not tokens; no hard total context cap"}
    if metrics['text_bytes'] >= 192 * 1024:
        # Byte pressure is explicit, never passed off as a token measurement.
        last['content'][0]['text'] += ('\nContext pressure: current textual request exceeds 192 KiB UTF-8 '
            '(byte estimate, not token count). At this complete tool boundary, use manage_builder_context '
            'to keep concise conclusions and archive redundant historical references. Current facts and native pairing remain mandatory.')
    return messages, metrics


def scrub(value):
    """Business-only audit: no private reasoning, authentication, or inline images."""
    if isinstance(value, dict):
        return {key: scrub(child) for key, child in value.items() if key.lower() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [scrub(child) for child in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return encoded(scrub(json.loads(value)))
            except (ValueError, TypeError):
                pass
        return _IMAGE.sub("[image omitted]", value)
    return value


class BusinessAudit:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "builder-audit"
        self.sequence = None

    def write(self, kind, value):
        if (self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)()
                or self.directory.resolve().parent != self.root.resolve()):
            raise OSError("unsafe audit directory")
        self.directory.mkdir(exist_ok=True)
        if self.sequence is None:
            self.sequence = max((int(path.stem) for path in self.directory.glob("*.json")
                                 if re.fullmatch(r"\d{8}", path.stem)), default=0)
        self.sequence += 1
        path = self.directory / f"{self.sequence:08d}.json"
        raw = encoded({"sequence": self.sequence, "kind": kind, "value": scrub(value)}).encode("utf-8")
        with path.open("xb") as stream:
            stream.write(raw)
        return {"sequence": self.sequence, "path": "builder-audit/" + path.name,
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def read(self, reference):
        if not isinstance(reference, dict) or not re.fullmatch(r"builder-audit/\d{8}\.json", reference.get("path", "")):
            raise ValueError("invalid audit reference")
        path = self.root / reference["path"]
        if (path.is_symlink() or self.directory.is_symlink()
                or getattr(self.directory, "is_junction", lambda: False)()
                or self.directory.resolve().parent != self.root.resolve()
                or path.resolve().parent != self.directory.resolve()):
            raise OSError("unsafe audit path")
        raw = path.read_bytes()
        if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError("changed audit")
        return json.loads(raw)
