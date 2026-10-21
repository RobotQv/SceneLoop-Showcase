"""Explicit frozen native intake; ordinary .blend imports never consult this."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .asset_jobs import controlled_path, file_facts
from .asset_provider import AssetJobError
from .blender_adapter import BlenderError, is_imported_manifest, validate_scene_document


def normalize_source(value):
    from .runtime import AppError
    if not isinstance(value, dict) or set(value) != {"workspace", "plan_sha256", "blend_sha256"}:
        raise AppError("native_candidate需要workspace及plan/blend的冻结SHA256。", "native_candidate_invalid")
    workspace = value["workspace"]
    if (not isinstance(workspace, str) or not workspace or len(workspace) > 4096
            or any(ord(character) < 32 for character in workspace)
            or workspace.startswith(("\\\\", "//")) or "://" in workspace
            or not Path(workspace).is_absolute() or ".." in Path(workspace).parts):
        raise AppError("native候选需要明确的本地绝对workspace。", "native_candidate_invalid")
    if any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
           for key in ("plan_sha256", "blend_sha256")):
        raise AppError("native候选摘要无效。", "native_candidate_invalid")
    return {"workspace": str(Path(workspace)), "plan_sha256": value["plan_sha256"],
            "blend_sha256": value["blend_sha256"]}


def verify_source(source):
    """Recheck bytes and the plan; embedded workspace/asset checks belong to fork."""
    from .runtime import AppError
    workspace = Path(source["workspace"])
    try:
        raw, plan_facts = file_facts(controlled_path(workspace, "plan.json"), 2 * 1024 * 1024)
        _, blend_facts = file_facts(controlled_path(workspace, "scene.blend"), 512 * 1024 * 1024)
        if (plan_facts["sha256"] != source["plan_sha256"]
                or blend_facts["sha256"] != source["blend_sha256"]):
            raise AppError("native来源在冻结后改变，已停止导入。", "native_candidate_changed", 409)
        plan = json.loads(raw)
        if is_imported_manifest(plan):
            raise AppError("native入口只接受参数化ScenePlan；普通文件使用path导入。", "native_candidate_invalid")
        validate_scene_document(plan)
    except (AssetJobError, BlenderError, OSError, ValueError, TypeError) as error:
        raise AppError("native来源不是有效的冻结参数化候选。", "native_candidate_invalid", 409) from error
    return workspace, plan
