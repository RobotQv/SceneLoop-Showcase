"""Read-only extraction of one object/part mesh from a frozen Blender candidate.

Runs inside Blender only. Never saves, never migrates part metadata and never
reconstructs geometry. Topology and size limits live in the pure contract module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bpy

from geometry_inspection_contract import (REPRESENTATIONS, build_measurement,
                                          within_extraction_limits)

PART_ID = "sceneloop_part_id"
PART_ROLE = "sceneloop_part_role"
IMPORT_ID = "sceneloop_import_id"
IMPORT_TEMPLATE = "sceneloop_import_template"


def _hash(path):
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _feature_labels(root):
    raw = root.get("requested_features")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
    return []


def _arrays(scene, target, representation):
    if representation not in REPRESENTATIONS:
        raise ValueError("representation 只支持 original 或 evaluated。")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = None
    if representation == "original":
        mesh, matrix = target.data, target.matrix_world
    else:
        evaluated = target.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        matrix = evaluated.matrix_world
    try:
        counts = (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))
        data = {
            "counts": counts,
            "matrix_world": [[round(float(value), 6) for value in row] for row in matrix],
            "unit_system": scene.unit_settings.system,
            "unit_scale_length": round(float(scene.unit_settings.scale_length), 6),
        }
        if within_extraction_limits(*counts):
            data["vertices"] = [[round(float(v.co.x), 6), round(float(v.co.y), 6), round(float(v.co.z), 6)] for v in mesh.vertices]
            data["edges"] = [[int(e.vertices[0]), int(e.vertices[1])] for e in mesh.edges]
            data["faces"] = [[int(index) for index in polygon.vertices] for polygon in mesh.polygons]
            data["face_normals"] = [[round(float(polygon.normal.x), 6), round(float(polygon.normal.y), 6),
                                     round(float(polygon.normal.z), 6)] for polygon in mesh.polygons]
        return data
    finally:
        if evaluated is not None:
            evaluated.to_mesh_clear()


def _generated_target(scene, object_id, part_id):
    matches = [obj for obj in scene.objects if obj.get("sceneloop_id") == object_id]
    if not matches:
        raise ValueError(f"该版本没有对象 {object_id}。")
    if len(matches) > 1:
        raise ValueError(f"该版本有 {len(matches)} 个对象使用重复的 sceneloop_id {object_id}；拒绝按遍历顺序选择。")
    root = matches[0]
    children = list(root.children_recursive)
    if part_id is None:
        candidates = [child for child in children if child.type == "MESH" and child.get(PART_ID)]
        if not candidates:
            raise ValueError(f"对象 {object_id} 没有带稳定 part_id 的 MESH 部件。")
        if len(candidates) > 1:
            raise ValueError(f"对象 {object_id} 包含多个部件；请提供 part_id，不静默合并。")
        target = candidates[0]
        resolved = target.get(PART_ID)
    else:
        matches = [child for child in children if child.get(PART_ID) == part_id]
        if len(matches) > 1:
            raise ValueError(f"对象 {object_id} 中有 {len(matches)} 个部件使用重复的 part_id {part_id}；拒绝按遍历顺序选择。")
        if not matches:
            raise ValueError(f"对象 {object_id} 中没有唯一的稳定部件 {part_id}。")
        target = matches[0]
        resolved = part_id
    if target.type != "MESH":
        raise ValueError("几何检查只支持 MESH 部件。")
    return root, target, resolved


def _imported_target(scene, object_id, part_id):
    matches = [obj for obj in scene.objects if obj.get(IMPORT_ID) == object_id]
    if len(matches) != 1:
        raise ValueError(f"该版本没有唯一的导入对象 {object_id}。")
    root = matches[0]
    if root.get(IMPORT_TEMPLATE):
        children = [child for child in root.children_recursive if child.type == "MESH" and child.get(PART_ID)]
        if part_id is None:
            if len(children) == 1:
                return root, children[0], children[0].get(PART_ID)
            if not children:
                raise ValueError("该参数化模板没有可解析的稳定部件 ID。")
            raise ValueError("该参数化模板包含多个部件；请提供 part_id，不静默合并。")
        found = [child for child in children if child.get(PART_ID) == part_id]
        if len(found) > 1:
            raise ValueError(f"模板 {object_id} 中有 {len(found)} 个部件使用重复的 part_id {part_id}；拒绝按遍历顺序选择。")
        if not found:
            raise ValueError(f"模板 {object_id} 中没有唯一的稳定部件 {part_id}。")
        return root, found[0], part_id
    if part_id is not None:
        raise ValueError("该导入对象没有可解析的部件层级；不要提供 part_id。")
    if root.type != "MESH":
        raise ValueError("几何检查只支持 MESH 对象。")
    return root, root, None


def _measure(scene, workspace, root, target, representation, object_id, part_id, declared, scene_kind):
    bpy.context.view_layer.update()
    data = _arrays(scene, target, representation)
    vertex_count, edge_count, polygon_count = data["counts"]
    return build_measurement(
        scene_kind=scene_kind, object_id=object_id, part_id=part_id, representation=representation,
        object_name=target.name, object_type=target.type,
        unit_system=data["unit_system"], unit_scale_length=data["unit_scale_length"],
        matrix_world=data["matrix_world"], blend_sha256=_hash(Path(workspace) / "scene.blend"),
        vertex_count=vertex_count, edge_count=edge_count, polygon_count=polygon_count,
        vertices=data.get("vertices"), edges=data.get("edges"), faces=data.get("faces"),
        face_normals=data.get("face_normals"), declared=declared)


def inspect_generated(scene, workspace, request):
    object_id = request["object_id"]
    part_id = request.get("part_id")
    representation = request.get("representation", "evaluated")
    root, target, resolved = _generated_target(scene, object_id, part_id)
    declared = {"object_name": root.name, "object_kind": root.get("sceneloop_kind"),
                "part_name": target.name, "role": target.get(PART_ROLE, ""),
                "requested_features": _feature_labels(root)}
    return _measure(scene, workspace, root, target, representation, object_id, resolved, declared, "generated")


def inspect_imported(scene, manifest, workspace, request):
    object_id = request["object_id"]
    part_id = request.get("part_id")
    representation = request.get("representation", "evaluated")
    root, target, resolved = _imported_target(scene, object_id, part_id)
    row = next((item for item in manifest.get("objects", []) if item.get("id") == object_id), {})
    declared = {"object_name": row.get("name"), "object_kind": row.get("kind"),
                "part_name": target.name, "role": target.get(PART_ROLE, "")}
    return _measure(scene, workspace, root, target, representation, object_id, resolved, declared, "imported")
