"""Pure request validation and bounded topology assembly for geometry inspection.

This module never touches Blender or scene files. The Blender worker extracts
mesh data and calls ``build_measurement``; the Builder/Judge tool layers merge
the legal run/revision identity and assign the citable evidence ID.
"""
from __future__ import annotations

import copy
import json
import math
import re

SCHEMA = "sceneloop.geometry-inspection.v1"
REPRESENTATIONS = ("original", "evaluated")
SIDES = ("before", "current")
MAX_VERTICES = 2048
MAX_EDGES = 8192
MAX_FACES = 4096
MAX_BYTES = 256 * 1024
MAX_TOPOLOGY_ELEMENTS = 200000
MAX_GEOMETRY_REQUESTS = 2

_SLUG = re.compile(r"[A-Za-z0-9_-]{1,80}")
_REVISION = re.compile(r"r[0-9]{3,6}")

METHODS = {
    "original": "Original mesh datablock read from the frozen .blend; modifiers NOT applied. Coordinates are mesh-local.",
    "evaluated": "Evaluated mesh read from the frozen .blend; visible modifiers applied. Coordinates are mesh-local.",
}

DEFINITIONS = {
    "boundary_edge": "恰好被 1 个面使用的边；只说明缺少第二个相邻面，不证明存在开孔或结构缺陷。",
    "wire_edge": "不被任何面使用的边。",
    "nonmanifold_edges": "被 3 个或更多面使用的边。",
    "nonmanifold_edges_including_boundary": "boundary 边与 >=3 面边之和；两种边含义不同。",
    "isolated_vertices": "不出现在任何边中的顶点。",
    "connected_components": "通过边连通的顶点分组数；孤立顶点各自计为一个分量。",
}

NOT_MEASURED = [
    "opening/hole semantics：boundary 或法线不能证明开孔、窗或框体存在",
    "interior/cabin completeness：不检查内部空间、内饰或遮挡",
    "collision/contact/walkability：不做精确相交、支撑或通路检测",
    "material/lighting/transmission：不读取材质节点、贴图或外观",
    "face orientation correctness：法线由同一 mesh 提供，朝向语义未验证",
]

LIMITATIONS = [
    "拓扑统计只描述取得的这份网格；boundary/wire/non-manifold 不等于缺陷或验收通过。",
    "mesh-local 坐标必须结合 matrix_world 才能得到世界坐标；不要把 authored 规格当实测。",
    "details.available=false 表示数组超限被省略，不是网格为空或不存在。",
    "original 与 evaluated 是不同来源；索引只对同一 source hash 与 representation 有效。",
]


def _slug(value, label):
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError(f"{label} 需要 1–80 位字母、数字、下划线或短横线。")
    return value


def _representation(value):
    if value not in REPRESENTATIONS:
        raise ValueError("representation 只支持 original 或 evaluated。")
    return value


def validate_builder_request(arguments):
    """Validate inspect_geometry arguments from the Builder model call."""
    if not isinstance(arguments, dict):
        raise ValueError("几何检查需要一个参数对象。")
    if set(arguments) - {"revision_id", "object_id", "part_id", "representation"}:
        raise ValueError("几何检查只接受 revision_id、object_id、part_id、representation。")
    revision_id = arguments.get("revision_id")
    if not isinstance(revision_id, str) or not _REVISION.fullmatch(revision_id):
        raise ValueError("revision_id 需要本 Run 的 rNNN 编号。")
    object_id = _slug(arguments.get("object_id"), "object_id")
    part_id = arguments.get("part_id")
    if part_id is not None:
        part_id = _slug(part_id, "part_id")
    representation = _representation(arguments.get("representation", "evaluated"))
    result = {"revision_id": revision_id, "object_id": object_id, "representation": representation}
    if part_id is not None:
        result["part_id"] = part_id
    return result


def validate_judge_request(arguments):
    """Validate inspect_geometry arguments from the independent Judge."""
    if not isinstance(arguments, dict):
        raise ValueError("几何检查需要一个参数对象。")
    if set(arguments) - {"side", "object_id", "part_id", "representation"}:
        raise ValueError("裁判几何检查只接受 side、object_id、part_id、representation。")
    side = arguments.get("side")
    if side not in SIDES:
        raise ValueError("side 只支持 before 或 current。")
    object_id = _slug(arguments.get("object_id"), "object_id")
    part_id = arguments.get("part_id")
    if part_id is not None:
        part_id = _slug(part_id, "part_id")
    representation = _representation(arguments.get("representation", "evaluated"))
    result = {"side": side, "object_id": object_id, "representation": representation}
    if part_id is not None:
        result["part_id"] = part_id
    return result


def within_extraction_limits(vertex_count, edge_count, polygon_count):
    return (vertex_count <= MAX_VERTICES and edge_count <= MAX_EDGES and polygon_count <= MAX_FACES)


def evidence_id(*, revision_id, object_id, part_id=None, representation="evaluated",
                session=None, index=None, side=None):
    """Stable citable ID bound to identity and representation; unversioned stays explicit."""
    fields = ["geometry"]
    if session:
        fields.append(session)
    if index is not None:
        fields.append(str(index))
    if side:
        fields.append(side)
    fields.append(revision_id if isinstance(revision_id, str) and revision_id else "unversioned")
    fields.append(object_id)
    if part_id:
        fields.append(part_id)
    fields.append(representation)
    return "-".join(fields)


def _counts(*values):
    for value in values:
        if type(value) is not int or value < 0:
            raise ValueError("网格计数需要非负整数。")
    return values


def _matrix(value):
    if (not isinstance(value, list) or len(value) != 4
            or any(not isinstance(row, list) or len(row) != 4 for row in value)):
        raise ValueError("matrix_world 需要 4x4 数组。")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
           for row in value for item in row):
        raise ValueError("matrix_world 包含无效数值。")
    return [[float(item) for item in row] for row in value]


def _arrays(vertex_count, edge_count, polygon_count, vertices, edges, faces, face_normals):
    if vertices is None and edges is None and faces is None:
        return None
    if not isinstance(vertices, list) or len(vertices) != vertex_count:
        raise ValueError("vertices 数量与 vertex_count 不一致。")
    if not isinstance(edges, list) or len(edges) != edge_count:
        raise ValueError("edges 数量与 edge_count 不一致。")
    if not isinstance(faces, list) or len(faces) != polygon_count:
        raise ValueError("faces 数量与 polygon_count 不一致。")
    for vertex in vertices:
        if not isinstance(vertex, list) or len(vertex) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vertex):
            raise ValueError("vertices 需要有限的三维坐标。")
    for edge in edges:
        if (not isinstance(edge, list) or len(edge) != 2 or any(type(index) is not int or not 0 <= index < vertex_count for index in edge)):
            raise ValueError("edges 需要两个有效顶点索引。")
    for face in faces:
        if (not isinstance(face, list) or len(face) < 3 or any(type(index) is not int or not 0 <= index < vertex_count for index in face)):
            raise ValueError("faces 需要有效的顶点索引环。")
    if face_normals is not None:
        if not isinstance(face_normals, list) or len(face_normals) != polygon_count:
            raise ValueError("face_normals 数量与 polygon_count 不一致。")
        for normal in face_normals:
            if not isinstance(normal, list) or len(normal) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in normal):
                raise ValueError("face_normals 需要有限的三维向量。")
    return vertices, edges, faces, face_normals, True


def topology_statistics(vertices, edges, faces):
    """Boundary/wire/non-manifold counts and connected components from raw arrays."""
    parent = list(range(len(vertices)))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    incidence = {}
    for index, edge in enumerate(edges):
        a, b = int(edge[0]), int(edge[1])
        key = (a, b) if a <= b else (b, a)
        incidence[key] = incidence.get(key, 0)
        union(a, b)
    for face in faces:
        count = len(face)
        for position in range(count):
            a, b = int(face[position]), int(face[(position + 1) % count])
            key = (a, b) if a <= b else (b, a)
            incidence[key] = incidence.get(key, 0) + 1
            union(a, b)
    boundary = sum(1 for value in incidence.values() if value == 1)
    wire = sum(1 for value in incidence.values() if value == 0)
    nonmanifold = sum(1 for value in incidence.values() if value >= 3)
    used = set()
    for edge in edges:
        used.add(int(edge[0]))
        used.add(int(edge[1]))
    isolated = sum(1 for index in range(len(vertices)) if index not in used)
    components = len({find(index) for index in range(len(vertices))}) if vertices else 0
    return {
        "boundary_edges": boundary,
        "wire_edges": wire,
        "nonmanifold_edges": nonmanifold,
        "nonmanifold_edges_including_boundary": boundary + nonmanifold,
        "isolated_vertices": isolated,
        "connected_components": components,
    }


def _topology(vertex_count, edge_count, polygon_count, arrays):
    if arrays is None:
        return {"computed": False, "reason": "网格数组未提取（超限或不可用），未计算拓扑统计。",
                "boundary_edges": None, "wire_edges": None, "nonmanifold_edges": None,
                "nonmanifold_edges_including_boundary": None, "isolated_vertices": None,
                "connected_components": None, "definitions": copy.deepcopy(DEFINITIONS)}
    if edge_count + polygon_count > MAX_TOPOLOGY_ELEMENTS:
        return {"computed": False, "reason": f"边+面数量 {edge_count + polygon_count} 超过统计工作上限 {MAX_TOPOLOGY_ELEMENTS}。",
                "boundary_edges": None, "wire_edges": None, "nonmanifold_edges": None,
                "nonmanifold_edges_including_boundary": None, "isolated_vertices": None,
                "connected_components": None, "definitions": copy.deepcopy(DEFINITIONS)}
    vertices, edges, faces, _, _ = arrays
    statistics = topology_statistics(vertices, edges, faces)
    return {"computed": True, "reason": None, **statistics, "definitions": copy.deepcopy(DEFINITIONS)}


def build_measurement(*, scene_kind, object_id, part_id, representation, object_name, object_type,
                      unit_system, unit_scale_length, matrix_world, blend_sha256,
                      vertex_count, edge_count, polygon_count,
                      vertices=None, edges=None, faces=None, face_normals=None, declared=None):
    """Assemble one bounded measurement document; identity is merged later."""
    vertex_count, edge_count, polygon_count = _counts(vertex_count, edge_count, polygon_count)
    if representation not in REPRESENTATIONS:
        raise ValueError("representation 只支持 original 或 evaluated。")
    arrays = _arrays(vertex_count, edge_count, polygon_count, vertices, edges, faces, face_normals)
    topology = _topology(vertex_count, edge_count, polygon_count, arrays)
    detail_reason = None
    if arrays is not None:
        if vertex_count > MAX_VERTICES:
            detail_reason = f"顶点数 {vertex_count} 超过完整数组上限 {MAX_VERTICES}。"
        elif edge_count > MAX_EDGES:
            detail_reason = f"边数 {edge_count} 超过完整数组上限 {MAX_EDGES}。"
        elif polygon_count > MAX_FACES:
            detail_reason = f"面数 {polygon_count} 超过完整数组上限 {MAX_FACES}。"
        else:
            bundle = {"vertices": vertices, "edges": edges, "faces": faces, "face_normals": face_normals}
            if len(json.dumps(bundle, separators=(",", ":"))) > MAX_BYTES:
                detail_reason = f"序列化数组超过 {MAX_BYTES} 字节上限。"
    else:
        if vertex_count > MAX_VERTICES:
            detail_reason = f"顶点数 {vertex_count} 超过完整数组上限 {MAX_VERTICES}（未提取数组）。"
        elif edge_count > MAX_EDGES:
            detail_reason = f"边数 {edge_count} 超过完整数组上限 {MAX_EDGES}（未提取数组）。"
        elif polygon_count > MAX_FACES:
            detail_reason = f"面数 {polygon_count} 超过完整数组上限 {MAX_FACES}（未提取数组）。"
        else:
            detail_reason = "网格数组未提供。"
    available = arrays is not None and detail_reason is None
    document_arrays = arrays if available else (None, None, None, None, False)
    polygon_sizes = [len(face) for face in document_arrays[2]] if available else None
    return {
        "schema": SCHEMA,
        "source": {"scene_kind": scene_kind, "object_id": object_id, "part_id": part_id,
                   "representation": representation, "method": METHODS[representation],
                   "blend_sha256": blend_sha256 or "unknown"},
        "target": {"object_name": object_name, "object_type": object_type},
        "coordinates": {
            "mesh_space": "coordinates are local to this mesh; apply matrix_world for world space",
            "world_rule": "world = matrix_world @ [x, y, z, 1]",
            "matrix_world": _matrix(matrix_world),
            "unit_system": unit_system,
            "unit_scale_length": unit_scale_length,
            "units_note": "SceneLoop generated plans use meters; imported files keep their own unit_scale_length value.",
        },
        "mesh": {"vertex_count": vertex_count, "edge_count": edge_count, "polygon_count": polygon_count,
                 "polygon_sizes": polygon_sizes, "vertices": document_arrays[0],
                 "edges": document_arrays[1], "faces": document_arrays[2], "face_normals": document_arrays[3]},
        "topology": topology,
        "details": {"available": available, "reason": detail_reason,
                    "max_vertices": MAX_VERTICES, "max_edges": MAX_EDGES, "max_faces": MAX_FACES,
                    "max_bytes": MAX_BYTES, "topology_work_limit": MAX_TOPOLOGY_ELEMENTS},
        "measured": [
            "目标网格的 vertex/edge/polygon 真实数量；通过时含完整 vertices/edges/faces（mesh-local）。",
            "matrix_world 与单位说明（coordinates）。",
            "boundary/wire/non-manifold 边计数与连通分量（拓扑定义见 topology.definitions）。",
            "face_normals 仅在提供时包含，来自同一份 mesh。",
        ],
        "not_measured": copy.deepcopy(NOT_MEASURED),
        "limitations": copy.deepcopy(LIMITATIONS),
        "declared": {"meaning": "Author/model labels for this target; not measurements.",
                     "target": copy.deepcopy(declared or {})},
    }


def compact_json(value):
    """Serialize exactly like the geometry tool messages and their size checks."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def document_bytes(document):
    return len(compact_json(document).encode("utf-8"))


def enforce_document_limit(document):
    """Bound the final serialized document; degrade arrays first, then labels."""
    if document_bytes(document) <= MAX_BYTES:
        return document
    result = copy.deepcopy(document)
    mesh = result.get("mesh")
    if isinstance(mesh, dict) and any(mesh.get(key) is not None for key in ("vertices", "edges", "faces", "face_normals")):
        mesh["vertices"] = mesh["edges"] = mesh["faces"] = mesh["face_normals"] = None
        mesh["polygon_sizes"] = None
        details = result.setdefault("details", {})
        details["available"] = False
        details["reason"] = (f"最终文档超过 {MAX_BYTES} 字节上限（含身份与说明）；"
                             "完整数组已省略，仅保留计数与已计算拓扑。")
    if document_bytes(result) > MAX_BYTES:
        declared = result.get("declared")
        if isinstance(declared, dict) and declared.get("target"):
            declared["target"] = {"omitted": f"最终文档超过 {MAX_BYTES} 字节上限"}
    if document_bytes(result) > MAX_BYTES:
        topology = result.get("topology")
        if isinstance(topology, dict):
            topology.pop("definitions", None)
    if document_bytes(result) > MAX_BYTES:
        raise ValueError("几何证据文档超过大小上限且无法安全降级。")
    return result


def finalize_document(measurement, *, identity, evidence_id_value=None):
    """Merge the legal run/revision identity and enforce the final document bound."""
    document = copy.deepcopy(measurement)
    source = copy.deepcopy(identity) if isinstance(identity, dict) else {}
    source.update(document.get("source") or {})
    document["source"] = source
    if evidence_id_value:
        document["evidence_id"] = evidence_id_value
    return enforce_document_limit(document)
