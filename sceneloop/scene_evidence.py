"""Side-effect-free evidence assembly for model-facing scene inputs.

Builder and Judge inputs must separate three different things:

* ``measured`` - numeric/structural values read from the frozen revision's raw
  inspector output (Blender evaluated geometry);
* ``declared`` - author/model written names, roles and requested features;
* ``editable_specification`` - authored plan parameters used to build a legal
  EditPlan, including imported manifests.

Raw ``plan.json``/``inspection.json`` documents, artifacts and hashes are never
modified; legacy documents are normalized in memory only.
"""
from __future__ import annotations

import copy
import json

from .asset_contract import AssetError, MAX_ASSETS, MAX_NODES, inspection as validate_asset_inspection, receipt as validate_asset_receipt

SCHEMA = "sceneloop.scene-evidence.v1"
DETAILS = ("summary", "full")

MEASURED_MEANING = (
    "Values read from this revision's raw inspector output. `method` and `field_sources` state how each "
    "value was measured; imported meshes report original mesh counts and evaluated bounds separately. "
    "Only this section contains measurements, and it still does not prove semantic capabilities.")
DECLARED_MEANING = (
    "Author/model written names, kinds, roles and requested features stored in the plan or the "
    ".blend. These are statements of intent: naming a feature does not make it exist and is not "
    "instrumentation evidence.")
SPECIFICATION_MEANING_GENERATED = (
    "Authored plan parameters for building a valid EditPlan. They are nominal and may not match "
    "current measured geometry; never report them as measurements or completion proof.")
SPECIFICATION_MEANING_IMPORTED = (
    "Editing baseline measured from the imported file (object transforms, dimensions, bounds and "
    "mesh metadata), plus the editable operations/fields and readonly reasons this import path "
    "allows. Mesh shape itself is not editable; do not treat these values as authored nominal "
    "parameters or as evaluated geometry for the mesh surface.")
SPECIFICATION_MEANING_UNKNOWN = (
    "Available plan/manifest parameters for building a valid EditPlan. Their provenance is not "
    "fully known; do not report them as measurements.")
VERIFICATION_MEANING = (
    "Tool execution and inspection boundaries. Successful execution only means the request was "
    "carried out; structural_only does not verify openings, interiors, collisions, walkability, "
    "materials or visual quality.")
DERIVED_MEANING = (
    "Host-side verification records for controlled cut_box subtractions. The record is stored in the "
    "actual .blend and re-checked against the current original mesh: it is current only while the mesh "
    "signature matches and the part has no modifiers. This is provenance plus bounded local "
    "measurements, never a model claim; a current cut does not prove openings, interiors, collisions, "
    "game function or visual quality.")
DERIVED_RESTRICTIONS = [
    "派生 cut 部件的真实网格不能通过 shape/vertices/faces/profile/depth/points/radius/sides 参数替换重建；",
    "含派生部件的 root 不能整体更换 kind，也不能用完整 parts 覆盖；",
    "仍允许 role/position/rotation/dimensions 变换、继续 cut_box、编辑其他部件，以及显式 remove_part/remove。",
]

GENERATED_FIELD_SOURCES = {
    "bounds": "evaluated world-space bounding box (depsgraph evaluated bound_box)",
    "mesh_parts": "count of evaluated mesh parts under the semantic object",
    "parts[].vertices/polygons": "evaluated mesh counts via to_mesh (modifiers applied)",
    "parts[].bounds": "evaluated world-space part bounding box",
    "parts[].position/rotation/scale/matrix_world": "actual object/part transform data",
    "parts[].derived_cut": "host cut_box receipt re-verified against the current original mesh signature; current only while valid and the part has no modifiers",
    "frame": "persistent authored-origin frame stored on the semantic root",
    "modifier_count/modifier_types": "modifier stack types; their effect is included in evaluated bounds",
    "material_count": "number of material slots",
}
IMPORTED_FIELD_SOURCES = {
    "bounds": "world-space bound_box of the evaluated object; for modifiers it can be looser than the deformed mesh surface",
    "dimensions": "extent computed from the evaluated bounds; managed templates aggregate child bounds",
    "mesh_vertices": "original mesh data vertex count (len(obj.data.vertices)); modifiers are NOT applied",
    "position": "object origin in world space",
    "rotation": "object rotation in degrees (object-level data)",
    "scale": "object scale (object-level data, not evaluated geometry size)",
    "modifier_count/modifier_types": "modifier stack types; modifier effects appear only in the evaluated bounds",
    "managed_template": "SceneLoop-managed template flag",
    "material_count": "number of material slots; names stay in the declared section",
}

BOUNDARIES = {
    "tool_success": "工具返回 ok=true 只表示工具协议执行完成，不等于目标、结构或质量通过。",
    "structural_only": "检查器确认语义对象与部件存在且包围盒有限；这不是结构、开孔或功能验收。",
    "openings": "现有检查器没有通用开孔/孔洞语义检测；role、features 或名称提到 openings 不构成开孔已验证。",
    "interior": "没有检查器证明内部空间、内饰或可行走性存在。",
    "collision_support": "ground/bounds 规则只是轴对齐包围盒推断；没有精确碰撞、支撑或穿插测量。",
    "materials": "材质槽名称不证明透射、发光或外观；typed surface检查如有则另列实测图状态与灯光事实，缺检查保持missing/unknown，视觉质量仍须实际appearance图片及适用阶段评审。",
    "labels": "对象、部件和材质 ID/名称是可定位标签；glass/window 等名称不证明对应效果。",
    "plan_values": "计划 position/dimensions/parts 是编辑规格或 nominal 参数，不是当前实测几何。",
    "absence_of_warnings": "没有 warning 不等于没有碰撞、遮挡或结构缺陷。",
    "imported_inventory": "导入清单的变换/尺寸/bounds 来自 Blender inventory 测量，但仍不证明语义或功能。",
    "derived_geometry": "受控 cut_box 的宿主核验只在记录签名与当前 original mesh 一致且目标无新增 modifier 时算当前测量；它只证明本次受限局部减料，不证明开孔语义、内部空间、碰撞或视觉质量。",
}

_MEASURED_OBJECT_FIELDS = ("bounds", "dimensions", "position", "rotation", "scale", "mesh_parts",
                           "mesh_count", "part_count", "mesh_vertices", "type", "managed_template")
_MEASURED_PART_FIELDS = ("part_id", "type", "parent_part_id", "bounds", "position", "rotation",
                         "scale", "matrix_world", "vertices", "polygons")
_SPEC_SCENE_FIELDS = ("schema_version", "title", "description", "assumptions", "camera",
                      "scene_name", "scene_id", "frame", "unit_system", "unit_scale_length",
                      "all_scenes", "comparison")


def _objects(document):
    rows = document.get("objects") if isinstance(document, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _semantic_rows(inspection):
    rows = inspection.get("semantic_objects") if isinstance(inspection, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def scene_kind(plan, inspection):
    for document in (inspection, plan):
        if isinstance(document, dict):
            value = document.get("scene_kind")
            if value in {"generated", "parametric"}:
                return "generated"
            if value == "imported":
                return "imported"
    return "generated" if _objects(plan) else "unknown"


def artifact_refs(run, revision_id, kinds=("blend", "plan", "inspection")):
    """Registered artifact identity for one revision; no file access, no mutation."""
    refs = []
    for artifact in (run or {}).get("artifacts", []):
        if not isinstance(artifact, dict) or artifact.get("revision_id") != revision_id:
            continue
        if artifact.get("kind") not in kinds:
            continue
        ref = {"kind": artifact.get("kind"), "id": artifact.get("id"), "sha256": artifact.get("sha256")}
        if artifact.get("view_id"):
            ref["view_id"] = artifact["view_id"]
        refs.append(ref)
    return refs


def _parametric_object_ids(plan, inspection):
    identifiers = [row.get("id") for row in _objects(plan)]
    identifiers += [row.get("id") for row in _semantic_rows(inspection)]
    return [value for index, value in enumerate(identifiers)
            if isinstance(value, str) and value not in identifiers[:index]]


def _asset_rows(plan, inspection):
    registered = (plan.get("assets") or {}).get("instances") if isinstance(plan.get("assets"), dict) else None
    measured = inspection.get("assets")
    return (registered if isinstance(registered, list) else [], measured if isinstance(measured, list) else [])


def known_object_ids(plan, inspection):
    identifiers = _parametric_object_ids(plan, inspection)
    declared, measured = _asset_rows(plan or {}, inspection or {})
    for row in (declared + measured)[:2 * MAX_ASSETS]:
        identifier = row.get("instance_id") if isinstance(row, dict) else None
        if isinstance(identifier, str) and 1 <= len(identifier) <= 80 and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def _assets(plan, inspection, *, detail, measured_detail, focus_ids):
    """Separate registered identity from inspector measurements; never edit specs."""
    registered, inspected = _asset_rows(plan, inspection)
    if not registered and not inspected:
        return None
    declared, measured, seen = [], [], set()
    truncated = len(registered) > MAX_ASSETS or len(inspected) > MAX_ASSETS
    registered_ids = {row.get("instance_id") for row in registered[:MAX_ASSETS]
                      if isinstance(row, dict) and isinstance(row.get("instance_id"), str)}
    rows = registered[:MAX_ASSETS] + [{"instance_id": row["instance_id"]}
        for row in inspected[:MAX_ASSETS] if isinstance(row, dict)
        and isinstance(row.get("instance_id"), str) and row["instance_id"] not in registered_ids]
    truncated = truncated or len(rows) > MAX_ASSETS
    for row in rows[:MAX_ASSETS]:
        if not isinstance(row, dict):
            truncated = True
            continue
        identifier = row.get("instance_id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 80:
            truncated = True
            continue
        if focus_ids is not None and identifier not in focus_ids:
            continue
        if identifier in seen:
            truncated = True
            continue
        seen.add(identifier)
        actual = {"instance_id": identifier, "available": False,
                  "reason": "missing_or_invalid_asset_measurement"}
        declared_row = {"instance_id": identifier, "available": False,
                        "reason": "invalid_asset_registration" if identifier in registered_ids else "missing_asset_registration"}
        try:
            validate_asset_receipt(row)
            declared_row = {key: copy.deepcopy(row[key]) for key in ("instance_id", "root_id", "source", "transform")}
            declared_row["node_count"] = len(row["nodes"])
            if detail == "full":
                declared_row["nodes"] = copy.deepcopy(row["nodes"][:MAX_NODES])
            matches = [item for item in inspected[:MAX_ASSETS]
                       if isinstance(item, dict) and item.get("instance_id") == identifier]
            if len(matches) == 1:
                validate_asset_inspection(matches[0], row)
                actual = {key: copy.deepcopy(matches[0][key]) for key in
                          ("instance_id", "root_id", "source", "transform", "matrix_world", "bounds",
                           "mesh_count", "vertices", "polygons")}
                actual.update(available=True, node_count=len(matches[0]["nodes"]))
                if measured_detail == "full":
                    actual["nodes"] = [{key: copy.deepcopy(node[key]) for key in
                                        ("node_id", "source_index", "parent_id", "type", "matrix_world")}
                                       for node in matches[0]["nodes"][:MAX_NODES]]
        except (AssetError, KeyError, TypeError, ValueError):
            pass  # An invalid source/pose never becomes fabricated measured data.
        candidate = {"declared": declared + [declared_row], "measured": measured + [actual]}
        if len(json.dumps(candidate).encode("utf-8")) > 65536:
            truncated = True
            for value in (declared_row, actual):
                if "nodes" in value:
                    value.pop("nodes")
                    value["node_details_omitted"] = True
            candidate = {"declared": declared + [declared_row], "measured": measured + [actual]}
        if len(json.dumps(candidate).encode("utf-8")) > 65536:
            break
        declared.append(declared_row)
        measured.append(actual)
    return declared, measured, {
        "truncated": truncated, "max_instances": MAX_ASSETS, "max_nodes_per_instance": MAX_NODES,
        "max_bytes": 65536,
        "source_meaning": "source SHA/byte_size is registered provenance, not a re-read of the original GLB.",
        "measured_meaning": "Actual frozen inspector root/node transforms, world bounds and static mesh counts; no mesh/material content signature or quality proof.",
        "editing": "Assets are read-only evidence in these Builder tools; not parametric editable objects.",
        "unregistered_measurements": sum(isinstance(row, dict) and row.get("instance_id") not in registered_ids
                                        for row in inspected[:MAX_ASSETS]),
    }


def source_identity(source=None, kind="unknown"):
    """Public source identity block for non-plan evidence such as geometry reads."""
    return _source(source, kind)


def _source(source, kind):
    source = source if isinstance(source, dict) else {}
    run_id = source.get("run_id")
    run_id = run_id if isinstance(run_id, str) and run_id.strip() else "unknown"
    if "revision_id" in source:
        revision_id = source.get("revision_id")
        if isinstance(revision_id, str) and revision_id.strip():
            revision_status = "numbered"
            revision_note = "该 Run 内的编号候选版本。"
        elif revision_id is None:
            revision_status = "unversioned"
            revision_note = "已知的无编号版本（candidate 工作区）；null 是合法身份，不是缺失。"
        else:
            revision_id, revision_status = "unknown", "unknown"
            revision_note = "来源提供的 revision 身份无效。"
    else:
        revision_id, revision_status = "unknown", "unknown"
        revision_note = "来源未提供 revision 身份。"
    working_version = source.get("working_version")
    if type(working_version) is not int or working_version < 0:
        working_version = None
    claimed = source.get("working_scene")
    if claimed is True and working_version is not None:
        committed, note = True, f"该版本已提交为 Working Version {working_version}；身份来自项目登记。"
    elif claimed is False:
        committed, note = False, "该版本不是已提交的 Working Scene；不声明 Working Version。"
    else:
        committed, note = None, "来源未提供 Working Scene 映射；Working Version 为 unknown。"
    result = {
        "run_id": run_id,
        "revision_id": revision_id,
        "revision_id_status": revision_status,
        "revision_id_note": revision_note,
        "scene_kind": kind,
        "working_scene": committed,
        "working_version": working_version if committed else None,
        "working_version_note": note,
    }
    base = source.get("base_selection")
    if isinstance(base, dict):
        result["base_selection"] = copy.deepcopy(base)
    base_revision = source.get("base_revision")
    if isinstance(base_revision, str) and base_revision:
        result["base_revision"] = base_revision
    note = source.get("note")
    if isinstance(note, str) and note:
        result["note"] = note
    artifacts = source.get("artifacts")
    if isinstance(artifacts, list):
        result["artifacts"] = copy.deepcopy(artifacts)
    return result


def _measured_labels(row, measured):
    materials = row.get("materials")
    if isinstance(materials, list):
        measured["material_count"] = len(materials)
    modifiers = row.get("modifiers")
    if isinstance(modifiers, list):
        measured["modifier_count"] = len(modifiers)
        measured["modifier_types"] = [item.get("type") for item in modifiers if isinstance(item, dict)]


def _current_derived(part):
    derived = part.get("derived_geometry")
    if not isinstance(derived, dict) or not derived.get("valid"):
        return None
    receipt = derived.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("before"), dict):
        return None
    return {"status": "current", "operation": copy.deepcopy(receipt.get("operation")),
            "before": copy.deepcopy(receipt.get("before")), "after": copy.deepcopy(receipt.get("after")),
            "entry_checks": copy.deepcopy(receipt.get("entry_checks")),
            "coordinates": receipt.get("coordinates"), "method": receipt.get("method")}


def _derived_part(part, *, detail):
    derived = part.get("derived_geometry")
    if not isinstance(derived, dict):
        return None
    receipt = derived.get("receipt") if isinstance(derived.get("receipt"), dict) else {}
    current = bool(derived.get("valid"))
    record = {"part_id": part.get("part_id") or part.get("id"),
              "status": derived.get("status") or "unknown", "current": current}
    if isinstance(derived.get("reason"), str) and derived["reason"]:
        record["reason"] = derived["reason"]
    if isinstance(receipt.get("operation"), dict):
        record["operation"] = copy.deepcopy(receipt["operation"])
    if isinstance(receipt.get("limitations"), list):
        record["limitations"] = copy.deepcopy(receipt["limitations"])
    postconditions = _current_derived(part)
    if postconditions is not None:
        if detail == "full":
            postconditions["samples"] = copy.deepcopy(receipt.get("samples"))
            postconditions["tolerance"] = copy.deepcopy(receipt.get("tolerance"))
        else:
            postconditions.pop("entry_checks", None)
        record["measured_postconditions"] = postconditions
    elif receipt:
        record["note"] = "核验记录仍作为来源保留，但当前网格/条件已变化，不能当作当前测量。"
    return record


def _derived_parts(inspection, *, detail, focus_ids=None):
    parts = []
    for row in _semantic_rows(inspection):
        if focus_ids is not None and row.get("id") not in focus_ids:
            continue
        for part in row.get("parts") if isinstance(row.get("parts"), list) else []:
            if not isinstance(part, dict):
                continue
            record = _derived_part(part, detail=detail)
            if record is not None:
                record["object_id"] = row.get("id")
                parts.append(record)
    return parts


def _measured_part(part, *, detail="summary"):
    result = {key: copy.deepcopy(part[key]) for key in _MEASURED_PART_FIELDS if key in part}
    if "part_id" not in result and isinstance(part.get("id"), str):
        result["part_id"] = part["id"]
    _measured_labels(part, result)
    derived = _current_derived(part)
    if derived is not None:
        result["derived_cut"] = derived
    return result


def _measured_object(row, *, detail):
    measured = {key: copy.deepcopy(row[key]) for key in _MEASURED_OBJECT_FIELDS if key in row}
    _measured_labels(row, measured)
    if isinstance(row.get("frame"), dict):
        measured["frame"] = copy.deepcopy(row["frame"])
    parts = row.get("parts")
    if isinstance(parts, list):
        measured["part_count"] = len(parts)
        measured["part_details_available"] = True
        if detail == "full":
            measured["parts"] = [_measured_part(part, detail=detail) for part in parts if isinstance(part, dict)]
    else:
        measured["part_details_available"] = False
    return {"id": row.get("id"), "measured": measured}


def _part_labels(source_row, inspection_row):
    labels = {}
    for row, role_keys in ((inspection_row, ("role",)), (source_row, ("role",))):
        for part in (row.get("parts") if isinstance(row.get("parts"), list) else []):
            if not isinstance(part, dict):
                continue
            identifier = part.get("part_id") or part.get("id")
            if not isinstance(identifier, str):
                continue
            entry = labels.setdefault(identifier, {"part_id": identifier})
            for key in role_keys:
                if isinstance(part.get(key), str) and part[key] and "role" not in entry:
                    entry["role"] = part[key]
            if isinstance(part.get("name"), str) and part["name"]:
                entry.setdefault("name", part["name"])
            if isinstance(part.get("materials"), list) and part["materials"]:
                entry.setdefault("material_labels", copy.deepcopy(part["materials"]))
    return [entry for entry in labels.values() if len(entry) > 1]


def _declared_object(plan_row, inspection_row):
    plan_row, inspection_row = plan_row or {}, inspection_row or {}
    result = {"id": plan_row.get("id") or inspection_row.get("id")}
    name = plan_row.get("name") or inspection_row.get("name")
    if isinstance(name, str) and name:
        result["name"] = name
    kind = plan_row.get("kind") or inspection_row.get("kind")
    if isinstance(kind, str) and kind:
        result["kind"] = kind
    if "key_object" in plan_row:
        result["key_object"] = bool(plan_row["key_object"])
    elif "key_object" in inspection_row:
        result["key_object"] = bool(inspection_row["key_object"])
    features = plan_row.get("features")
    if not isinstance(features, list):
        features = inspection_row.get("requested_features")
    if isinstance(features, list) and features:
        result["requested_features"] = copy.deepcopy(features)
    shape_status = inspection_row.get("shape_status")
    if isinstance(shape_status, str) and shape_status:
        result["shape_status"] = shape_status
    materials = inspection_row.get("materials")
    if not isinstance(materials, list):
        materials = plan_row.get("materials")
    if isinstance(materials, list) and materials:
        result["material_labels"] = copy.deepcopy(materials)
    labels = _part_labels(plan_row, inspection_row)
    if labels:
        result["part_labels"] = labels
    return result


def _specification(plan, *, detail, focus_ids, kind):
    rows = []
    for row in _objects(plan):
        if focus_ids is not None and row.get("id") not in focus_ids:
            continue
        item = copy.deepcopy(row)
        if detail != "full":
            parts = item.get("parts")
            if isinstance(parts, list):
                item["planned_part_count"] = len(parts)
                item["planned_details_available"] = True
            item.pop("parts", None)
        rows.append(item)
    scene = {key: copy.deepcopy(plan[key]) for key in _SPEC_SCENE_FIELDS
             if isinstance(plan, dict) and key in plan}
    meaning = {"imported": SPECIFICATION_MEANING_IMPORTED,
               "generated": SPECIFICATION_MEANING_GENERATED}.get(kind, SPECIFICATION_MEANING_UNKNOWN)
    return {"meaning": meaning, "scene": scene, "objects": rows}


def _verification(inspection, warnings):
    return {
        "meaning": VERIFICATION_MEANING,
        "inspector_verification": inspection.get("verification") or "unknown",
        "visual_acceptance": inspection.get("visual_acceptance") or "unknown",
        "rule_based_warnings": {
            "count": len(warnings),
            "note": BOUNDARIES["absence_of_warnings"],
        },
        "boundaries": copy.deepcopy(BOUNDARIES),
    }


def build_scene_evidence(plan, inspection, *, source=None, object_ids=None, detail="summary", measured_parts=None):
    """Assemble measured/declared/editable sections without mutating the inputs.

    ``object_ids`` restricts the document to a focused set (per-object inspection)
    and is reported under ``focus``. ``detail='full'`` keeps complete editable plan
    parameters; ``measured_parts=True`` keeps full measured part rows independently
    of the editable-specification detail (the Judge needs measurements, not edit
    meshes). ``summary`` drops part detail but retains the authored part count.
    """
    if detail not in DETAILS:
        raise ValueError(f"未知证据详细程度：{detail}")
    if measured_parts is not None and not isinstance(measured_parts, bool):
        raise ValueError("measured_parts 需要布尔值")
    measured_detail = detail if measured_parts is None else ("full" if measured_parts else "summary")
    plan = plan if isinstance(plan, dict) else {}
    inspection = inspection if isinstance(inspection, dict) else {}
    kind = scene_kind(plan, inspection)
    focus_ids = None
    if object_ids is not None:
        if not isinstance(object_ids, (list, tuple, set)) or any(not isinstance(value, str) for value in object_ids):
            raise ValueError("object_ids 需要字符串序列")
        focus_ids = list(dict.fromkeys(object_ids))
    identifiers = _parametric_object_ids(plan, inspection)
    if focus_ids is not None:
        identifiers = [value for value in identifiers if value in focus_ids]
    plan_index = {row.get("id"): row for row in _objects(plan)}
    inspected_index = {row.get("id"): row for row in _semantic_rows(inspection)}
    measured_objects = []
    declared_objects = []
    for identifier in identifiers:
        inspected, planned = inspected_index.get(identifier), plan_index.get(identifier)
        measured_objects.append(_measured_object(inspected, detail=measured_detail) if inspected
                                else {"id": identifier, "measured": {"available": False,
                                "reason": "该版本检查产物中没有此对象的测量记录"}})
        declared_objects.append(_declared_object(planned, inspected))
    warnings = copy.deepcopy(inspection.get("warnings")) if isinstance(inspection.get("warnings"), list) else []
    if kind == "imported":
        units = (f"Blender inventory values in the file's own unit system; unit_scale_length="
                 f"{plan.get('unit_scale_length', 'unknown')}")
        method = ("Imported-scene inventory on this frozen revision. Bounds/dimensions are evaluated; "
                  "mesh_vertices is the original mesh data with modifiers NOT applied; transforms are "
                  "object-level data. field_sources state each source.")
        field_sources = IMPORTED_FIELD_SOURCES
    elif kind == "generated":
        units = "SceneLoop plan/world units (meters); imported scenes keep their own units"
        method = ("Blender depsgraph on this frozen revision. Bounds and part mesh counts are evaluated; "
                  "transforms are actual object/part data. field_sources state each source.")
        field_sources = GENERATED_FIELD_SOURCES
    else:
        units = "unknown"
        method = "Legacy inspection without a recognizable scene kind; field provenance is unknown."
        field_sources = {}
    document = {
        "schema": SCHEMA,
        "source": _source(source, kind),
        "measured": {
            "meaning": MEASURED_MEANING,
            "method": method,
            "units": units,
            "field_sources": copy.deepcopy(field_sources),
            "scene": {
                key: copy.deepcopy(inspection[key]) for key in
                ("object_count", "actual_object_count", "mesh_count", "verification", "visual_acceptance")
                if key in inspection
            },
            "objects": measured_objects,
            "warnings": warnings,
            "limitations": copy.deepcopy(inspection.get("limitations")) if isinstance(inspection.get("limitations"), list) else [],
        },
        "declared": {"meaning": DECLARED_MEANING, "objects": declared_objects},
        "derived_geometry": {
            "meaning": DERIVED_MEANING,
            "edit_restrictions": copy.deepcopy(DERIVED_RESTRICTIONS),
            "parts": _derived_parts(inspection, detail=measured_detail, focus_ids=focus_ids),
            **({} if detail == "full" else {
                "detail_access": "摘要只列派生部件 ID/状态与当前测量；完整采样通过 inspect_scene(object_ids) 或 inspect_geometry 获取。"}),
        },
        "editable_specification": _specification(plan, detail=detail, focus_ids=focus_ids, kind=kind),
        "verification": _verification(inspection, warnings),
    }
    if focus_ids is not None:
        document["focus"] = {"object_ids": focus_ids}
    assets = _assets(plan, inspection, detail=detail, measured_detail=measured_detail, focus_ids=focus_ids)
    if assets is not None:
        document["declared"]["assets"], document["measured"]["assets"], document["verification"]["asset_projection"] = assets
    if 'surface' in plan:
        import hashlib
        expected=hashlib.sha256(json.dumps(plan['surface'],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        actual=inspection.get('surface') or {}
        verified=(actual.get('verified') is True and actual.get('state')==plan['surface'] and actual.get('state_digest')==expected)
        document['declared']['surface']=copy.deepcopy(plan['surface'])
        document['measured']['surface']={'status':'verified' if verified else 'unknown',
            'meaning':'Actual material graph and lighting inspection; declarations alone are not appearance quality proof.',
            'state_digest':actual.get('state_digest') if verified else None,
            'materials':copy.deepcopy(actual.get('materials',[])) if verified else [],
            'lighting':copy.deepcopy(actual.get('lighting')) if verified else None}
    else:
        document['measured']['surface']={'status':'missing','meaning':'No typed material/lighting inspection is available; use actual appearance images and report remaining uncertainty.'}
    return document
