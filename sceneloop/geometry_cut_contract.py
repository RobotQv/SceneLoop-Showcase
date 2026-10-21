"""Pure validation, coordinates and measurement helpers for controlled cut_box edits.

This module never imports bpy and never touches scene data. The Blender runtime
extracts real arrays and calls these helpers so the same contract is testable in
plain Python; the Inspector format and Blender execution stay authoritative.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re

CUT_OP = "cut_box"
MODES = ("opening", "recess")
AXES = ("X", "Y", "Z")
SIDES = ("min", "max")
AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
AXIS_SECTION = {"X": (1, 2), "Y": (0, 2), "Z": (0, 1)}

OPENING_FIELDS = {"op", "id", "part_id", "mode", "axis", "center", "size"}
RECESS_FIELDS = {"op", "id", "part_id", "mode", "axis", "side", "center", "size", "depth"}

MAX_CUTS_PER_BATCH = 4
MAX_TARGET_VERTICES = 2048
MAX_TARGET_EDGES = 8192
MAX_TARGET_POLYGONS = 4096
MAX_RECEIPT_BYTES = 32768
MAX_RECEIPT_COUNT = 1000000
MAX_RECEIPT_VOLUME = 1.0e12
MAX_RECEIPT_VALUE = 100000.0
COORDINATE_LIMIT = 10000.0

ABSOLUTE_TOLERANCE = 1e-5
TOLERANCE_RATIO = 1e-4
POINT_MATCH_TOLERANCE = 1e-5
MIN_VOLUME_RATIO = 1e-9
MIN_VOLUME_ABSOLUTE = 1e-12
MARGIN_RATIO = 0.02
MIN_MARGIN = 1e-4
MIN_REMAINING_RATIO = 0.05
MIN_REMAINING = 1e-4
SAMPLE_FRACTIONS = (0.15, 0.5, 0.85)
OUTSIDE_FACTOR = 0.25
SIGNATURE_PRECISION = 6

COORDINATES_NOTE = "target original mesh-local coordinates; not world metres"

RECEIPT_LIMITATIONS = [
    "只对该部件 original mesh-local 的这次操作有效；修改 mesh、增加 modifier、shape key 或替换部件后失效。",
    "局部采样是 3x3 内侧通道点加四条孔外对照点，不证明全场景无自交、无碰撞或完整语义正确。",
    "cut 通过只表示本次受限减料通过核验，不代表开孔/凹槽在该物体或场景中合适，也不代表质量验收。",
    "world 位置由当前 matrix_world 决定；记录数值不是世界米数。",
]

_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")


def _slug(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} 需要 1–80 位字母、数字、下划线或短横线。")
    return value


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 需要有限数值。")
    if isinstance(value, int) and abs(value) > int(COORDINATE_LIMIT):
        # Compare huge ints as integers so math.isfinite never overflows.
        raise ValueError(f"{label} 超出局部坐标范围。")
    if not math.isfinite(value):
        raise ValueError(f"{label} 需要有限数值。")
    if abs(value) > COORDINATE_LIMIT:
        raise ValueError(f"{label} 超出局部坐标范围。")
    if positive and value <= 0:
        raise ValueError(f"{label} 必须为正数。")
    return float(value)


def _pair(value, label, *, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} 需要两个数值。")
    return [_number(item, label, positive=positive) for item in value]


def normalize_cut(operation):
    """Validate one cut_box operation and return the canonical request."""
    if not isinstance(operation, dict) or operation.get("op") != CUT_OP:
        raise ValueError("cut_box 操作格式无效。")
    mode = operation.get("mode")
    if mode not in MODES:
        raise ValueError("cut_box mode 只支持 opening 或 recess。")
    expected = RECESS_FIELDS if mode == "recess" else OPENING_FIELDS
    if set(operation) != expected:
        raise ValueError("cut_box 字段不匹配：" + "、".join(sorted(expected)))
    result = {
        "op": CUT_OP,
        "id": _slug(operation["id"], "id"),
        "part_id": _slug(operation["part_id"], "part_id"),
        "mode": mode,
        "axis": None,
        "center": _pair(operation["center"], "center"),
        "size": _pair(operation["size"], "size", positive=True),
    }
    if operation["axis"] not in AXES:
        raise ValueError("cut_box axis 只支持 X / Y / Z。")
    result["axis"] = operation["axis"]
    if mode == "recess":
        if operation["side"] not in SIDES:
            raise ValueError("recess side 只支持 min 或 max。")
        result["side"] = operation["side"]
        result["depth"] = _number(operation["depth"], "depth", positive=True)
    return result


def is_cut(operation):
    return isinstance(operation, dict) and operation.get("op") == CUT_OP


def section_points(cut):
    """Return (inside 3x3 grid, four outside control points) in mesh-local 3D."""
    axis = AXIS_INDEX[cut["axis"]]
    u, v = AXIS_SECTION[cut["axis"]]
    cu, cv = cut["center"]
    su, sv = cut["size"]
    inside = []
    for fraction_u in SAMPLE_FRACTIONS:
        for fraction_v in SAMPLE_FRACTIONS:
            point = [0.0, 0.0, 0.0]
            point[u] = cu + (fraction_u - 0.5) * su
            point[v] = cv + (fraction_v - 0.5) * sv
            inside.append(point)
    outside = []
    for offset_u, offset_v in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        point = [0.0, 0.0, 0.0]
        point[u] = cu + offset_u * su * (0.5 + OUTSIDE_FACTOR)
        point[v] = cv + offset_v * sv * (0.5 + OUTSIDE_FACTOR)
        outside.append(point)
    return inside, outside


def local_bounds(vertices):
    if not vertices:
        raise ValueError("目标网格为空。")
    return [[min(float(vertex[i]) for vertex in vertices) for i in range(3)],
            [max(float(vertex[i]) for vertex in vertices) for i in range(3)]]


def validate_cut_bounds(cut, bounds):
    """Reject cuts without surrounding material or without valid control samples.

    Only the two real section coordinates are compared with the mesh-local
    bounds; the axial coordinate of a sample is decided by the runtime ray, so a
    translated mesh whose axial interval excludes 0 stays fully supported.
    """
    lo, hi = bounds
    axis = AXIS_INDEX[cut["axis"]]
    u, v = AXIS_SECTION[cut["axis"]]
    margins = {}
    for index, sample_index in ((u, 0), (v, 1)):
        extent = hi[index] - lo[index]
        if not math.isfinite(extent) or extent <= ABSOLUTE_TOLERANCE:
            raise ValueError("目标在该截面方向没有可测量的厚度。")
        margin = max(MIN_MARGIN, MARGIN_RATIO * extent)
        low = cut["center"][sample_index] - cut["size"][sample_index] / 2
        high = cut["center"][sample_index] + cut["size"][sample_index] / 2
        if low < lo[index] + margin - ABSOLUTE_TOLERANCE or high > hi[index] - margin + ABSOLUTE_TOLERANCE:
            raise ValueError("切口必须保留周围实体余量，不能触及或跨出目标边界。")
        margins[cut["axis"] + ("uv"[sample_index])] = round(margin, 6)
    if cut["mode"] == "recess":
        extent = hi[axis] - lo[axis]
        if not math.isfinite(extent) or extent <= ABSOLUTE_TOLERANCE:
            raise ValueError("目标没有可用的轴向厚度。")
        remaining = extent - cut["depth"]
        if remaining <= max(MIN_REMAINING, MIN_REMAINING_RATIO * extent) + ABSOLUTE_TOLERANCE:
            raise ValueError("盲槽 depth 必须保留足够的背壁厚度。")
    # Control points must stay in the section material, otherwise the outside
    # check is meaningless. The runtime computes their axial position.
    _, outside = section_points(cut)
    for point in outside:
        for index in (u, v):
            if point[index] < lo[index] - ABSOLUTE_TOLERANCE or point[index] > hi[index] + ABSOLUTE_TOLERANCE:
                raise ValueError("孔外对照采样点落在目标之外；请缩小切口或移动 center。")
    return margins


def mesh_signature(vertices, edges, faces, *, precision=SIGNATURE_PRECISION):
    """Deterministic signature from actual arrays; never names, counts or plan text."""
    digest = hashlib.sha256()
    format_string = f"{{:.{precision}f}}"
    for vertex in vertices:
        digest.update(("v" + ",".join(format_string.format(float(value)) for value in vertex) + ";").encode("utf-8"))
    for edge in edges:
        digest.update(("e" + ",".join(str(int(index)) for index in edge) + ";").encode("utf-8"))
    for face in faces:
        digest.update(("f" + ",".join(str(int(index)) for index in face) + ";").encode("utf-8"))
    return digest.hexdigest()


def triangle_volume(vertices, faces):
    """Absolute signed volume of the closed mesh in mesh-local units."""
    total = 0.0
    for face in faces:
        if len(face) < 3:
            continue
        origin = vertices[face[0]]
        for index in range(1, len(face) - 1):
            a, b = vertices[face[index]], vertices[face[index + 1]]
            total += (origin[0] * (a[1] * b[2] - a[2] * b[1])
                      - origin[1] * (a[0] * b[2] - a[2] * b[0])
                      + origin[2] * (a[0] * b[1] - a[1] * b[0])) / 6.0
    return abs(total)


def mesh_stats(vertices, edges, faces):
    return {"signature": mesh_signature(vertices, edges, faces), "vertices": len(vertices),
            "edges": len(edges), "polygons": len(faces),
            "volume": round(triangle_volume(vertices, faces), 9)}


def mesh_health(vertices, edges, faces):
    """Return a rejection reason unless the target is a closed, manifold, single solid."""
    if not vertices or not edges or not faces:
        raise ValueError("目标网格为空或不完整。")
    for vertex in vertices:
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in vertex):
            raise ValueError("目标网格包含无效坐标。")
    incidence = {}
    for face in faces:
        if len(face) < 3 or len(set(face)) != len(face):
            raise ValueError("目标网格包含退化面。")
        count = len(face)
        for position in range(count):
            a, b = int(face[position]), int(face[(position + 1) % count])
            if not 0 <= a < len(vertices) or not 0 <= b < len(vertices):
                raise ValueError("目标网格包含无效索引。")
            key = (a, b) if a <= b else (b, a)
            incidence[key] = incidence.get(key, 0) + 1
    edge_use = {}
    for edge in edges:
        key = (int(edge[0]), int(edge[1]))
        key = key if key[0] <= key[1] else (key[1], key[0])
        if key in edge_use:
            raise ValueError("目标网格包含重复边。")
        edge_use[key] = incidence.get(key, 0)
    for count in edge_use.values():
        if count == 0:
            raise ValueError("目标网格包含悬空边，首版只支持闭合实体。")
        if count != 2:
            raise ValueError("目标网格不是闭合边流形实体，首版不支持。")
    for key in incidence:
        if key not in edge_use:
            raise ValueError("面引用了不存在的边，目标网格无效。")
    # Degenerate-area faces (fan cross products all zero).
    for face in faces:
        origin = vertices[face[0]]
        normal = [0.0, 0.0, 0.0]
        for position in range(1, len(face) - 1):
            a, b = vertices[face[position]], vertices[face[position + 1]]
            cross = [(a[1] - origin[1]) * (b[2] - origin[2]) - (a[2] - origin[2]) * (b[1] - origin[1]),
                     (a[2] - origin[2]) * (b[0] - origin[0]) - (a[0] - origin[0]) * (b[2] - origin[2]),
                     (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])]
            normal = [normal[i] + cross[i] for i in range(3)]
        if sum(value * value for value in normal) < 1e-20:
            raise ValueError("目标网格包含面积为零的面。")
    parent = list(range(len(vertices)))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for key in edge_use:
        left, right = find(key[0]), find(key[1])
        if left != right:
            parent[right] = left
    components = {find(index) for index in range(len(vertices))}
    if len(components) != 1:
        raise ValueError("目标网格不是单一连通实体，首版不支持。")
    return None


def validate_target_limits(vertices, edges, faces):
    if len(vertices) > MAX_TARGET_VERTICES:
        raise ValueError(f"目标顶点数 {len(vertices)} 超过 cut_box 上限 {MAX_TARGET_VERTICES}。")
    if len(edges) > MAX_TARGET_EDGES:
        raise ValueError(f"目标边数 {len(edges)} 超过 cut_box 上限 {MAX_TARGET_EDGES}。")
    if len(faces) > MAX_TARGET_POLYGONS:
        raise ValueError(f"目标面数 {len(faces)} 超过 cut_box 上限 {MAX_TARGET_POLYGONS}。")


def build_receipt(request, *, before, after, samples, entry_checks, target=None, tolerance_used=None):
    receipt = {
        "schema_version": 1,
        "operation": copy.deepcopy(request),
        "target": copy.deepcopy(target) if target is not None else {
            "object_id": request["id"], "part_id": request["part_id"]},
        "representation": "original",
        "coordinates": COORDINATES_NOTE,
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "samples": copy.deepcopy(samples),
        "entry_checks": copy.deepcopy(entry_checks),
        "tolerance": {"absolute": ABSOLUTE_TOLERANCE, "margin_ratio": MARGIN_RATIO,
                      "min_margin": MIN_MARGIN, "min_remaining_ratio": MIN_REMAINING_RATIO,
                      "min_remaining": MIN_REMAINING},
        "method": "temporary box Difference/Exact boolean on an isolated mesh copy, then local ray/BVH checks",
        "limitations": copy.deepcopy(RECEIPT_LIMITATIONS),
    }
    if tolerance_used is not None:
        receipt["tolerance_used"] = copy.deepcopy(tolerance_used)
    return receipt


def json_dumps(value):
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def receipt_bytes(receipt):
    try:
        return len(json_dumps(receipt).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_RECEIPT_BYTES + 1


def _finite_number(value, limit=MAX_RECEIPT_VALUE):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        # Integer comparison avoids OverflowError for huge JSON integers.
        return abs(value) <= limit
    return math.isfinite(value) and abs(value) <= limit


def _valid_hits(value):
    return isinstance(value, list) and len(value) <= 2 and all(_finite_number(item) for item in value)


def _stats_reason(value, label):
    if not isinstance(value, dict):
        return f"核验记录 {label} 统计无效"
    signature = value.get("signature")
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        return f"核验记录 {label} 签名无效"
    for field, limit in (("vertices", MAX_TARGET_VERTICES), ("edges", MAX_TARGET_EDGES),
                         ("polygons", MAX_TARGET_POLYGONS)):
        count = value.get(field)
        if type(count) is not int or not 0 <= count <= limit:
            return f"核验记录 {label} {field} 计数无效"
    volume = value.get("volume")
    if not _finite_number(volume, MAX_RECEIPT_VOLUME) or volume <= 0:
        return f"核验记录 {label} 体积无效"
    return None


def _distance(left, right):
    return max(abs(a - b) for a, b in zip(left, right))


def validate_receipt(receipt):
    """Strict, bounded validation of one stored receipt; returns a reason or None.

    The record is host metadata, so damaged or older data must be downgraded
    without raising. Beyond per-field bounds this binds the request, target
    identity, sampling axis/positions and coordinate contract together, because
    a correct mesh signature alone does not prove the recorded hole location.
    """
    if not isinstance(receipt, dict):
        return "核验记录不是对象"
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        return "核验记录版本无效"
    try:
        operation = normalize_cut(receipt.get("operation"))
    except ValueError as error:
        return f"核验记录中的请求无效：{error}"
    target = receipt.get("target")
    if target is not None:
        if not isinstance(target, dict) or set(target) != {"object_id", "part_id"}:
            return "核验记录目标身份无效"
        object_id, part_id = target.get("object_id"), target.get("part_id")
        if object_id != operation["id"] or part_id != operation["part_id"]:
            return "核验记录目标身份与请求不一致"
    if (not isinstance(operation["id"], str) or not _ID.fullmatch(operation["id"])
            or not isinstance(operation["part_id"], str) or not _ID.fullmatch(operation["part_id"])):
        return "核验记录目标身份无效"
    if receipt.get("representation") != "original":
        return "核验记录不是 original mesh-local 表示"
    if receipt.get("coordinates") != COORDINATES_NOTE:
        return "核验记录坐标系声明不符合 original mesh-local 契约"
    reason = _stats_reason(receipt.get("before"), "before") or _stats_reason(receipt.get("after"), "after")
    if reason:
        return reason
    tolerance = receipt.get("tolerance")
    if not isinstance(tolerance, dict) or not tolerance:
        return "核验记录缺少容差"
    for value in tolerance.values():
        if not _finite_number(value) or value <= 0:
            return "核验记录容差无效"
    tolerance_used = receipt.get("tolerance_used")
    if tolerance_used is not None:
        if (not isinstance(tolerance_used, dict) or not tolerance_used
                or set(tolerance_used) - {"axis_extent", "extent", "tolerance", "absolute"}):
            return "核验记录实际容差无效"
        for value in tolerance_used.values():
            if not _finite_number(value) or value <= 0:
                return "核验记录实际容差无效"
    samples = receipt.get("samples")
    if not isinstance(samples, dict):
        return "核验记录采样无效"
    if samples.get("axis") != operation["axis"]:
        return "核验记录采样轴与请求不一致"
    expected_inside, expected_outside = section_points(operation)
    for key, expected in (("inside", expected_inside), ("outside", expected_outside)):
        rows = samples.get(key)
        if not isinstance(rows, list) or len(rows) != len(expected):
            return f"核验记录采样 {key} 数量无效"
        seen = set()
        for row, target_point in zip(rows, expected):
            if not isinstance(row, dict):
                return "核验记录采样行无效"
            point = row.get("point")
            if (not isinstance(point, list) or len(point) != 3
                    or not all(_finite_number(value, COORDINATE_LIMIT) for value in point)):
                return "核验记录采样点无效"
            if _distance(point, target_point) > POINT_MATCH_TOLERANCE:
                return f"核验记录 {key} 采样点与请求的 center/size/axis 不一致"
            rounded = tuple(round(value, 6) for value in point)
            if rounded in seen:
                return f"核验记录 {key} 采样点重复，未覆盖整个切口截面"
            seen.add(rounded)
            if not _valid_hits(row.get("hits_before")) or not _valid_hits(row.get("hits_after")):
                return "核验记录采样命中无效"
    entry = receipt.get("entry_checks")
    if not isinstance(entry, dict):
        return "核验记录缺少入口检查"
    for key, count in (("inside_hits_before", 9), ("inside_hits_after", 9),
                       ("outside_hits_before", 4), ("outside_hits_after", 4)):
        rows = entry.get(key)
        if not isinstance(rows, list) or len(rows) != count or not all(_valid_hits(row) for row in rows):
            return f"核验记录 {key} 无效"
    if not all(len(row) == 2 and row == sorted(row) and row[1] - row[0] > 0
               for row in entry["inside_hits_before"]):
        return "核验记录切口内入口/背壁交点无效"
    if operation["mode"] == "opening":
        if any(entry["inside_hits_after"]):
            return "核验记录 opening 通道切后不能仍有交点"
    elif not all(len(row) == 2 and row == sorted(row) and row[1] - row[0] > 0
                 for row in entry["inside_hits_after"]):
        return "核验记录 recess 入口/背壁交点无效"
    for key in ("outside_hits_before", "outside_hits_after"):
        if not all(len(row) == 2 and row == sorted(row) and row[1] - row[0] > 0 for row in entry[key]):
            return "核验记录孔外对照必须包含前后两层有效交点"
    removed = entry.get("removed_volume")
    if not _finite_number(removed, MAX_RECEIPT_VOLUME) or removed <= 0:
        return "核验记录减料体积无效"
    for key in ("inside", "outside"):
        for sample, before, after in zip(samples[key], entry[f"{key}_hits_before"], entry[f"{key}_hits_after"]):
            if sample.get("hits_before") != before or sample.get("hits_after") != after:
                return "核验记录采样与入口检查不一致"
    limitations = receipt.get("limitations")
    if not isinstance(limitations, list) or not limitations or not all(isinstance(item, str) and item for item in limitations):
        return "核验记录限制说明无效"
    return None


def receipt_consistent(receipt, *, axis_extent):
    """Return a reason unless the record satisfies the promised postconditions.

    ``axis_extent`` is the actual target mesh-local thickness along the
    operation axis, so the comparison tolerance comes from a bounded host rule
    instead of values the record could inflate.
    """
    reason = validate_receipt(receipt)
    if reason is not None:
        return reason
    operation = normalize_cut(receipt["operation"])
    if not _finite_number(axis_extent, COORDINATE_LIMIT) or axis_extent <= ABSOLUTE_TOLERANCE:
        return "宿主轴向尺度无效，无法核验记录"
    host = max(ABSOLUTE_TOLERANCE, TOLERANCE_RATIO * axis_extent)
    used = receipt.get("tolerance_used")
    if isinstance(used, dict):
        declared = used.get("tolerance")
        if not _finite_number(declared) or declared <= 0:
            return "核验记录实际容差无效"
        if abs(declared - host) > max(ABSOLUTE_TOLERANCE, 0.5 * host):
            return "核验记录容差与宿主规则不一致"
    else:
        declared = receipt["tolerance"].get("absolute", host)
        if declared > max(host * 4.0, 4 * ABSOLUTE_TOLERANCE):
            return "核验记录容差超过宿主规则"
    tolerance = host
    before, after, entry = receipt["before"], receipt["after"], receipt["entry_checks"]
    drop = before["volume"] - after["volume"]
    removed = entry["removed_volume"]
    if abs(drop - removed) > max(ABSOLUTE_TOLERANCE, 1e-3 * before["volume"]):
        return "核验记录体积变化与 before/after 矛盾"
    if removed <= max(MIN_VOLUME_ABSOLUTE, MIN_VOLUME_RATIO * before["volume"]):
        return "核验记录减料体积低于下限"

    def matches(left, right):
        return len(left) == len(right) and all(abs(a - b) <= tolerance for a, b in zip(left, right))

    if operation["mode"] == "opening":
        if not all(matches(a, b) for a, b in zip(entry["outside_hits_before"], entry["outside_hits_after"])):
            return "核验记录 opening 孔外对照发生变化"
    else:
        direction = -1 if operation["side"] == "max" else 1
        remaining_floor = max(MIN_REMAINING, MIN_REMAINING_RATIO * axis_extent)
        for previous, current in zip(entry["inside_hits_before"], entry["inside_hits_after"]):
            if operation["side"] == "max":
                entry_before, back_before, entry_after, back_after = previous[1], previous[0], current[1], current[0]
            else:
                entry_before, back_before, entry_after, back_after = previous[0], previous[1], current[0], current[1]
            if abs(entry_after - (entry_before + direction * operation["depth"])) > tolerance:
                return "核验记录盲槽入口深度不一致"
            if abs(back_after - back_before) > tolerance:
                return "核验记录盲槽穿透背壁"
            if (current[1] - current[0]) <= remaining_floor:
                return "核验记录盲槽剩余背壁低于执行判据"
        if not all(matches(a, b) for a, b in zip(entry["outside_hits_before"], entry["outside_hits_after"])):
            return "核验记录盲槽外对照发生变化"
    return None
