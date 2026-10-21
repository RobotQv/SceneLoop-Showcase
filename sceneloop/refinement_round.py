"""Bounded local draft rounds; SQL and scene receipts remain the authority."""
from __future__ import annotations

import copy
import hashlib
import json
import re

MODE = "local_round_v1"
LIMITS = {"max_rounds": 6, "max_edit_attempts": 6, "max_brain_calls": 21,
          "max_tool_calls": 42, "max_observations": 18, "max_seconds": 900}
_ID = re.compile(r"[A-Za-z0-9_-]{12,128}\Z")


class RoundError(Exception):
    def __init__(self, message, code="round_invalid", status=400, *, current_version=None):
        super().__init__(message)
        self.code, self.status, self.current_version = code, status, current_version


def enabled(run):
    return (run.get("iteration") or {}).get("mode") == MODE


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RoundError(f"{name}需要1–{maximum}字文字。")
    return value.strip()


def criteria_snapshot(plan, item_ids):
    if (not isinstance(item_ids, list) or not item_ids or len(item_ids) > 32
            or any(not isinstance(item, str) for item in item_ids)
            or len(set(item_ids)) != len(item_ids)):
        raise RoundError("局部轮需要有效且不重复的Plan工作项。")
    index = {item["id"]: item for item in plan["items"]}
    if set(item_ids) - set(index):
        raise RoundError("局部轮引用了不存在的Plan工作项。", "round_criteria_changed", 409)
    return [{"id": identifier, "desired": copy.deepcopy(index[identifier]["desired"]),
             "criteria": copy.deepcopy(index[identifier]["criteria"])} for identifier in item_ids]


def validate_begin(body):
    if not isinstance(body, dict) or set(body) != {"goal", "criteria", "item_ids"}:
        raise RoundError("开始局部轮需要goal、criteria和item_ids。")
    return {"goal": text(body["goal"], "局部目标", 2000),
            "criteria": text(body["criteria"], "整轮自检标准", 2000),
            "item_ids": copy.deepcopy(body["item_ids"])}


def validate_resume(body):
    if (not isinstance(body, dict) or set(body) != {"resume_id", "expected_round_version"}
            or not isinstance(body.get("resume_id"), str) or not _ID.fullmatch(body["resume_id"])
            or type(body.get("expected_round_version")) is not int
            or body["expected_round_version"] < 1):
        raise RoundError("续做需要有效resume_id和expected_round_version。")
    return copy.deepcopy(body)


def validate_end(body):
    if (not isinstance(body, dict)
            or set(body) != {"end_id", "round_id", "expected_round_version", "reason"}
            or not isinstance(body.get("end_id"), str) or not _ID.fullmatch(body["end_id"])
            or not isinstance(body.get("round_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", body["round_id"])
            or type(body.get("expected_round_version")) is not int
            or body["expected_round_version"] < 1):
        raise RoundError("结束局部轮需要有效end_id、round_id、expected_round_version和reason。")
    return {**copy.deepcopy(body), "reason": text(body["reason"], "结束原因", 2000)}


def scene_semantics(plan, inspection):
    """Compare supported authored state, not revision-specific .blend bytes or renders."""
    if not isinstance(plan, dict) or not isinstance(inspection, dict):
        raise RoundError("场景语义证据无效。", "round_evidence_invalid", 409)
    if 'surface' in plan:
        actual=inspection.get('surface') or {}
        if (actual.get('verified') is not True or actual.get('state')!=plan['surface']
                or actual.get('state_digest')!=digest(plan['surface'])):
            raise RoundError('外观状态缺少当前实际检查证明。','round_evidence_invalid',409)
    if plan.get('assets',{}).get('instances'):
        from .asset_contract import registration,inspection as check_asset,AssetError
        try:
            registered=registration(plan['assets'],[o['id'] for o in plan['objects']])['instances']
            measured=inspection.get('assets')
            if not isinstance(measured,list) or len(measured)!=len(registered):raise AssetError('Missing actual asset measurements')
            for row in registered:
                matches=[v for v in measured if v.get('instance_id')==row['instance_id']]
                if len(matches)!=1:raise AssetError('Asset measurement identity conflict')
                check_asset(matches[0],row)
        except (AssetError,KeyError,TypeError) as error:
            raise RoundError('资产状态缺少当前实际检查证明。','round_evidence_invalid',409) from error
    if plan.get("scene_kind") == "imported":
        # The inventory is measured from the actual Blender scene. Edit
        # eligibility and fork-local scene/rig names are not edited content.
        fields = ("id", "name", "type", "kind", "position", "rotation", "scale",
                  "dimensions", "bounds", "managed_template", "mesh_vertices",
                  "modifiers", "materials")
        def authored(rows):
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise RoundError("导入场景缺少物体清单。", "round_evidence_invalid", 409)
            return [{field: copy.deepcopy(row[field]) for field in fields if field in row}
                    for row in rows]
        objects = authored(plan.get("objects"))
        if authored(inspection.get("semantic_objects")) != objects:
            raise RoundError("导入场景清单与检查结果不一致。", "round_evidence_invalid", 409)
        return {"kind": "imported", "objects": objects}
    derived = []
    for obj in inspection.get("semantic_objects", []):
        for part in obj.get("parts", []):
            if "derived_geometry" in part:
                derived.append({"object_id": obj.get("id"), "part_id": part.get("part_id"),
                                "geometry": copy.deepcopy(part["derived_geometry"])})
    derived.sort(key=lambda row: (str(row["object_id"]), str(row["part_id"])))
    return {"kind": "parametric", "plan": copy.deepcopy(plan), "derived_geometry": derived}
