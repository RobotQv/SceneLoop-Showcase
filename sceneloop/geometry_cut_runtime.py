"""Trusted Blender-side execution and verification of controlled cut_box edits.

Runs inside Blender only. The saved .blend and the original mesh arrays are the
authority; plans keep their nominal specification. A cut is accepted only after
host measurements of the actual output, and the receipt is re-verified against
the current mesh before it is ever reported as current evidence.
"""
from __future__ import annotations

import json

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from blender_adapter import BlenderError
from geometry_cut_contract import (ABSOLUTE_TOLERANCE, AXIS_INDEX, AXIS_SECTION, MAX_RECEIPT_BYTES,
                                   build_receipt, local_bounds, mesh_health, mesh_stats, normalize_cut,
                                   receipt_consistent, section_points, validate_cut_bounds,
                                   validate_receipt, validate_target_limits)

DERIVED_KEY = "sceneloop_derived_geometry"
PART_ID = "sceneloop_part_id"


def mesh_arrays(mesh):
    vertices = [[float(vertex.co.x), float(vertex.co.y), float(vertex.co.z)] for vertex in mesh.vertices]
    edges = [[int(edge.vertices[0]), int(edge.vertices[1])] for edge in mesh.edges]
    faces = [[int(index) for index in polygon.vertices] for polygon in mesh.polygons]
    return vertices, edges, faces


def object_arrays(obj):
    if obj.type != "MESH" or obj.data is None:
        raise BlenderError("cut_box 只支持 MESH 部件")
    return mesh_arrays(obj.data)


def _tolerance(extent):
    return max(ABSOLUTE_TOLERANCE, 1e-4 * extent)


def _bvh(vertices, faces):
    points = [tuple(vertex) for vertex in vertices]
    polygons = [list(face) for face in faces]
    try:
        return BVHTree.FromPolygons(points, polygons, all_triangles=False)
    except Exception as error:
        raise BlenderError(f"无法为目标网格建立查询结构：{error}") from None


def _axis_hits(bvh, section_point, axis, bounds):
    """All intersections along +axis, from outside the negative bound to outside the positive."""
    index = AXIS_INDEX[axis]
    lo, hi = bounds
    extent = hi[index] - lo[index]
    start = list(section_point)
    start[index] = lo[index] - max(1.0, extent * 0.5)
    origin = Vector(start)
    direction = Vector((0.0, 0.0, 0.0))
    direction[index] = 1.0
    hits = []
    cursor = origin.copy()
    for _ in range(64):
        location, _normal, _polygon, _distance = bvh.ray_cast(cursor, direction)
        if location is None:
            break
        hits.append(round(float(location[index]), 6))
        cursor = location + direction * 1e-5
    return hits


def _hits_match(left, right, tolerance):
    if len(left) != len(right):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _sample_state(bvh, cut, bounds):
    inside, outside = section_points(cut)
    return ([_axis_hits(bvh, point, cut["axis"], bounds) for point in inside],
            [_axis_hits(bvh, point, cut["axis"], bounds) for point in outside])


def _validate_entry(cut, bounds, inside_hits, outside_hits):
    index = AXIS_INDEX[cut["axis"]]
    extent = bounds[1][index] - bounds[0][index]
    tolerance = _tolerance(extent)
    all_hits = inside_hits + outside_hits
    for hits in all_hits:
        if len(hits) != 2:
            raise BlenderError("切口入口不是单层有限厚度区域（需要恰好两个轴向交点），首版不支持。")
        if hits[1] - hits[0] <= tolerance:
            raise BlenderError("切口位置厚度过小或擦边，无法可靠减料。")
    # The first version only supports an axial planar slice: both sampled
    # surfaces across inside and control points must each be one plane. A
    # slanted entry, a stepped/cavity region or an already-cut area is refused.
    near = [hits[0] for hits in all_hits]
    far = [hits[1] for hits in all_hits]
    if max(near) - min(near) > tolerance:
        raise BlenderError("切口一侧不是轴向平面（斜入口、阶梯或已有切口），首版不支持。")
    if max(far) - min(far) > tolerance:
        raise BlenderError("切口另一侧不是轴向平面（斜入口、阶梯或已有切口），首版不支持。")
    if cut["mode"] == "recess":
        depth = cut["depth"]
        for hits in inside_hits:
            thickness = hits[1] - hits[0]
            if depth >= thickness - max(1e-4, 0.05 * extent):
                raise BlenderError("盲槽 depth 太深，无法保留背壁。")
    return tolerance


def _expected_removal(cut, inside_hits):
    size_u, size_v = cut["size"]
    if cut["mode"] == "opening":
        thickness = sum(hits[1] - hits[0] for hits in inside_hits) / len(inside_hits)
        return size_u * size_v * thickness
    return size_u * size_v * cut["depth"]


def _post_validate(cut, bounds, tolerance, before_hits, after_hits, before_stats, after_stats):
    inside_before, outside_before = before_hits
    inside_after, outside_after = after_hits
    index = AXIS_INDEX[cut["axis"]]
    if cut["mode"] == "opening":
        for hits in inside_after:
            if hits:
                raise BlenderError("贯穿孔采样点仍然与实体相交，减料未贯通。")
        for before, after in zip(outside_before, outside_after):
            if not _hits_match(before, after, tolerance):
                raise BlenderError("孔外对照采样发生变化，切口越界或破坏了保留区域。")
    else:
        for hits in inside_after:
            if len(hits) != 2:
                raise BlenderError("盲槽采样点不是保留背壁的盲槽（交点数量异常）。")
        for before, after in zip(inside_before, inside_after):
            if cut["side"] == "min":
                entry_before, back_before = before[0], before[1]
                entry_after, back_after = after[0], after[1]
            else:
                entry_before, back_before = before[1], before[0]
                entry_after, back_after = after[1], after[0]
            if abs(entry_after - (entry_before + cut["depth"] * (1 if cut["side"] == "min" else -1))) > tolerance:
                raise BlenderError("盲槽入口深度与请求不一致。")
            if abs(back_after - back_before) > tolerance:
                raise BlenderError("盲槽穿透了背壁或改变了背面位置。")
            if (back_after - entry_after) * (1 if cut["side"] == "min" else -1) <= max(1e-4, 0.05 * (bounds[1][index] - bounds[0][index])):
                raise BlenderError("盲槽剩余背壁厚度不足。")
        for before, after in zip(outside_before, outside_after):
            if not _hits_match(before, after, tolerance):
                raise BlenderError("槽外对照采样发生变化，切口越界或破坏了保留区域。")
    reduction = before_stats["volume"] - after_stats["volume"]
    expected = _expected_removal(cut, inside_before)
    if reduction <= max(ABSOLUTE_TOLERANCE, expected * 0.5):
        raise BlenderError("实际体积没有有效减少，cut 未生效或与请求不符。")
    if after_stats["signature"] == before_stats["signature"]:
        raise BlenderError("输出网格与输入相同，cut 没有改变实际几何。")
    return reduction


def _cutter_box(cut, bounds):
    index = AXIS_INDEX[cut["axis"]]
    u, v = AXIS_SECTION[cut["axis"]]
    lo, hi = bounds
    extent = hi[index] - lo[index]
    overshoot = max(1.0, extent)
    if cut["mode"] == "opening":
        axial = (lo[index] - overshoot, hi[index] + overshoot)
    elif cut["side"] == "min":
        axial = (lo[index] - overshoot, lo[index] + cut["depth"])
    else:
        axial = (hi[index] - cut["depth"], hi[index] + overshoot)
    low = [0.0, 0.0, 0.0]
    high = [0.0, 0.0, 0.0]
    low[u], high[u] = cut["center"][0] - cut["size"][0] / 2, cut["center"][0] + cut["size"][0] / 2
    low[v], high[v] = cut["center"][1] - cut["size"][1] / 2, cut["center"][1] + cut["size"][1] / 2
    low[index], high[index] = axial
    return low, high


def _create_cutter(scene, target, low, high):
    """A closed box baked into world space from the target's exact matrix_world.

    Assigning a sheared matrix_world to an object would lose the shear in its
    loc/rot/scale decomposition, so the local box corners are transformed here.
    """
    corners = [(low[0], low[1], low[2]), (high[0], low[1], low[2]), (high[0], high[1], low[2]), (low[0], high[1], low[2]),
               (low[0], low[1], high[2]), (high[0], low[1], high[2]), (high[0], high[1], high[2]), (low[0], high[1], high[2])]
    matrix = target.matrix_world
    vertices = [tuple(matrix @ Vector(corner)) for corner in corners]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new("SceneLoop cut box")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("SceneLoop cut box", mesh)
    scene.collection.objects.link(obj)
    obj.matrix_world = Matrix.Identity(4)
    return obj


def _apply_boolean(scene, target, cut, bounds):
    low, high = _cutter_box(cut, bounds)
    cutter = _create_cutter(scene, target, low, high)
    modifier = None
    try:
        modifier = target.modifiers.new("SceneLoop cut", "BOOLEAN")
        modifier.operation = "DIFFERENCE"
        modifier.solver = "EXACT"
        modifier.object = cutter
        view_layer = bpy.context.view_layer
        for obj in view_layer.objects:
            obj.select_set(False)
        target.select_set(True)
        view_layer.objects.active = target
        if bpy.ops.object.modifier_apply(modifier=modifier.name) != {"FINISHED"}:
            raise BlenderError("布尔减料未完成，已拒绝保存。")
        modifier = None
    finally:
        if modifier is not None and target.modifiers.get(modifier.name):
            target.modifiers.remove(modifier)
        cutter_mesh = cutter.data
        bpy.data.objects.remove(cutter, do_unlink=True)
        if cutter_mesh is not None and cutter_mesh.users == 0:
            bpy.data.meshes.remove(cutter_mesh)


def _discard(target, original_data):
    """Restore the untouched datablock and drop the failed working copy."""
    working = target.data
    target.data = original_data
    if working is not None and working.users == 0:
        bpy.data.meshes.remove(working)


def _release_mesh(mesh):
    """Release the saved original once the cut is fully verified and recorded."""
    if mesh is None or mesh.users != 0:
        return
    try:
        bpy.data.meshes.remove(mesh)
    except Exception:
        # The datablock is no longer addressable; the committed cut stays valid.
        pass


def _write_receipt(target, receipt):
    payload = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise BlenderError("核验记录超过大小上限，未保存本次 cut。")
    target[DERIVED_KEY] = payload


def cut_target(scene, root, part_id, operation):
    """Execute one cut on the actual frozen candidate; raise BlenderError on rejection."""
    cut = None
    try:
        cut = normalize_cut(operation)
    except ValueError as error:
        raise BlenderError(str(error)) from None
    matches = [child for child in root.children if child.get(PART_ID) == part_id]
    if len(matches) != 1:
        raise BlenderError(f"目标 {part_id} 不是唯一的直系部件。")
    target = matches[0]
    if target.type != "MESH" or target.data is None:
        raise BlenderError("cut_box 只支持 MESH 部件。")
    if target.modifiers:
        raise BlenderError("目标部件带 modifier，首版不支持 cut_box；请先使用显式支持的编辑方式。")
    if target.data.shape_keys:
        raise BlenderError("目标部件带形态键，首版不支持 cut_box。")

    vertices, edges, faces = object_arrays(target)
    try:
        mesh_health(vertices, edges, faces)
        validate_target_limits(vertices, edges, faces)
        bounds = local_bounds(vertices)
        margins = validate_cut_bounds(cut, bounds)
    except ValueError as error:
        raise BlenderError(str(error)) from None
    before = mesh_stats(vertices, edges, faces)
    bvh = _bvh(vertices, faces)
    before_hits = _sample_state(bvh, cut, bounds)
    tolerance = _validate_entry(cut, bounds, before_hits[0], before_hits[1])
    extent_value = bounds[1][AXIS_INDEX[cut["axis"]]] - bounds[0][AXIS_INDEX[cut["axis"]]]

    original_data = target.data
    target.data = original_data.copy()
    try:
        _apply_boolean(scene, target, cut, bounds)
    except Exception:
        _discard(target, original_data)
        raise
    vertices_after, edges_after, faces_after = object_arrays(target)
    try:
        mesh_health(vertices_after, edges_after, faces_after)
        validate_target_limits(vertices_after, edges_after, faces_after)
    except ValueError as error:
        _discard(target, original_data)
        raise BlenderError(f"cut 输出不满足网格边界：{error}") from None
    after = mesh_stats(vertices_after, edges_after, faces_after)
    after_bvh = _bvh(vertices_after, faces_after)
    after_hits = _sample_state(after_bvh, cut, bounds)
    try:
        reduction = _post_validate(cut, bounds, tolerance, before_hits, after_hits, before, after)
    except BlenderError:
        _discard(target, original_data)
        raise
    entry_checks = {
        "inside_hits_before": before_hits[0],
        "outside_hits_before": before_hits[1],
        "inside_hits_after": after_hits[0],
        "outside_hits_after": after_hits[1],
        "removed_volume": round(reduction, 9),
        "margin_used": margins,
    }
    samples = {
        "method": "3x3 axial samples inside the cut plus four outside control points",
        "axis": cut["axis"],
        "inside": [{"point": [round(value, 6) for value in point], "hits_before": before_hits[0][index],
                    "hits_after": after_hits[0][index]} for index, point in enumerate(section_points(cut)[0])],
        "outside": [{"point": [round(value, 6) for value in point], "hits_before": before_hits[1][index],
                     "hits_after": after_hits[1][index]} for index, point in enumerate(section_points(cut)[1])],
    }
    receipt = build_receipt(cut, before=before, after=after, samples=samples, entry_checks=entry_checks,
                            target={"object_id": root.get("sceneloop_id"), "part_id": part_id},
                            tolerance_used={"axis_extent": round(extent_value, 9), "tolerance": round(tolerance, 9),
                                            "absolute": ABSOLUTE_TOLERANCE})
    reason = receipt_consistent(receipt, axis_extent=extent_value)
    if reason is not None:
        _discard(target, original_data)
        raise BlenderError(f"本次 cut 核验记录自检失败：{reason}")
    try:
        _write_receipt(target, receipt)
    except BlenderError:
        _discard(target, original_data)
        raise
    # The saved original must stay alive until every fallible check and the marker
    # write succeeded; only then may the isolated working copy's source be released.
    _release_mesh(original_data)
    return receipt


def derived_part_ids(root):
    return {child.get(PART_ID) for child in root.children
            if child.get(PART_ID) and DERIVED_KEY in child}


def has_derived_geometry(root):
    return any(DERIVED_KEY in child for child in root.children_recursive)


def part_has_derived(obj):
    return obj is not None and DERIVED_KEY in obj


def _stored_receipt(raw):
    if not isinstance(raw, str):
        return None, "核验标记不是文字记录，按未知处理。"
    if len(raw.encode("utf-8")) > MAX_RECEIPT_BYTES:
        return None, "核验记录超过大小上限，按无效处理。"
    try:
        receipt = json.loads(raw)
    except ValueError:
        return None, "核验记录损坏，按未知处理。"
    return receipt, None


def verify_derived_geometry(obj, *, object_id=None, part_id=None):
    """Re-verify one part's stored receipt against its current original mesh.

    Signature equality is necessary but not sufficient: the record's own
    counts, volume and postconditions are checked, its target identity must
    match, huge or damaged values are downgraded without raising, and any
    unsupported influence (modifier, shape key) invalidates the current claim.
    """
    if obj is None or DERIVED_KEY not in obj:
        return None
    raw = obj.get(DERIVED_KEY)
    result = {"status": "historical", "valid": False, "reason": None, "receipt": None}
    receipt, reason = _stored_receipt(raw)
    result["receipt"] = receipt
    if reason is not None:
        result["status"] = "invalid"
        result["reason"] = reason
        return result
    reason = validate_receipt(receipt)
    if reason is not None:
        result["status"] = "invalid"
        result["reason"] = reason
        return result
    operation = normalize_cut(receipt["operation"])
    target = receipt.get("target") or {"object_id": operation["id"], "part_id": operation["part_id"]}
    if object_id is not None and operation["id"] != object_id:
        result["status"] = "invalid"
        result["reason"] = "核验记录请求物体与当前部件不一致，按无效处理。"
        return result
    if part_id is not None and operation["part_id"] != part_id:
        result["status"] = "invalid"
        result["reason"] = "核验记录请求部件与当前部件不一致，按无效处理。"
        return result
    if object_id is not None and target.get("object_id") != object_id:
        result["status"] = "invalid"
        result["reason"] = "核验记录目标物体与当前部件不一致，按无效处理。"
        return result
    if part_id is not None and target.get("part_id") != part_id:
        result["status"] = "invalid"
        result["reason"] = "核验记录目标部件与当前部件不一致，按无效处理。"
        return result
    if obj.type != "MESH" or obj.data is None:
        result["reason"] = "标记存在但目标不再是 MESH。"
        return result
    if obj.modifiers:
        result["reason"] = "目标新增了 modifier，原 original mesh-local 核验失效。"
        return result
    if obj.data.shape_keys:
        result["reason"] = "目标新增了形态键，原 original mesh-local 核验失效。"
        return result
    vertices, edges, faces = mesh_arrays(obj.data)
    stats = mesh_stats(vertices, edges, faces)
    after = receipt["after"]
    if stats["signature"] != after["signature"]:
        result["reason"] = "当前网格签名与核验记录不一致，旧 cut 结论只作为历史来源。"
        return result
    if (stats["vertices"], stats["edges"], stats["polygons"]) != (after["vertices"], after["edges"], after["polygons"]):
        result["status"] = "invalid"
        result["reason"] = "当前网格计数与核验记录矛盾，按无效处理。"
        return result
    if abs(stats["volume"] - after["volume"]) > max(ABSOLUTE_TOLERANCE, 1e-6 * max(stats["volume"], after["volume"], 1.0)):
        result["status"] = "invalid"
        result["reason"] = "当前网格体积与核验记录矛盾，按无效处理。"
        return result
    bounds = local_bounds(vertices)
    axis_extent = bounds[1][AXIS_INDEX[operation["axis"]]] - bounds[0][AXIS_INDEX[operation["axis"]]]
    reason = receipt_consistent(receipt, axis_extent=axis_extent)
    if reason is not None:
        result["status"] = "invalid"
        result["reason"] = reason
        return result
    result["status"] = "current"
    result["valid"] = True
    return result
