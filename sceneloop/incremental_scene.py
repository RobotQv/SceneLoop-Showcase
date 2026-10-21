"""Incremental edits of frozen SceneLoop candidates; the saved mesh is authoritative.

Only the explicitly targeted object/part is replaced. This module never reconstructs
an entire scene from its ScenePlan and never operates on the user display scene.
"""
from __future__ import annotations

import copy
import json
import math
import re

import bpy
from mathutils import Euler, Matrix, Vector

from blender_adapter import BlenderError, apply_edit_plan, validate_plan
from geometry_contract import MAX_SCENE_FACES, MAX_SCENE_VERTICES
from geometry_cut_runtime import cut_target, has_derived_geometry, part_has_derived

PART_ID = "sceneloop_part_id"
PART_ROLE = "sceneloop_part_role"
ORIGIN = "sceneloop_part_origin_offset"
SCALE_BASIS = "sceneloop_api_scale_basis_v1"


def _part_id(spec, index):
    return spec.get("id", f"part_{index + 1:03d}")


def _members(root):
    return [root, *root.children_recursive]


def _roots(scene):
    roots = {}
    for obj in scene.objects:
        if "sceneloop_id" in obj:
            identifier = obj["sceneloop_id"]
            if identifier in roots:
                raise BlenderError(f"Duplicate scene object id: {identifier}")
            roots[identifier] = obj
    return roots


def _check_owned(scene, root, members=None):
    """Reject dependencies whose deletion/mutation could affect another object."""
    members = set(members or _members(root))
    permitted_users = members | {root}
    users = bpy.data.user_map()
    for obj in members:
        if obj != root and "sceneloop_id" in obj:
            raise BlenderError("Nested semantic objects cannot be edited as owned parts")
        if obj.library or obj.override_library or (obj.data and getattr(obj.data, "library", None)):
            raise BlenderError("Linked/overridden geometry is read-only")
        if len(obj.users_scene) != 1 or scene not in obj.users_scene:
            raise BlenderError("Target geometry is shared with another scene")
        if obj.animation_data or obj.constraints or (obj.data and getattr(obj.data, "animation_data", None)):
            raise BlenderError("Animated or constrained targets need an explicit supported editing operation")
        if any(not isinstance(user, bpy.types.Collection) and user != scene and user not in permitted_users for user in users.get(obj, set())):
            raise BlenderError("Target is referenced by objects/data outside its owned hierarchy")
        if any(collection.library for collection in obj.users_collection):
            raise BlenderError("Linked collections are read-only")
    for owner in bpy.data.objects:
        if owner.instance_type == "COLLECTION" and owner.instance_collection:
            if members.intersection(owner.instance_collection.all_objects):
                raise BlenderError("Target belongs to an instanced collection")


def ensure_part_metadata(root, item):
    """Upgrade legacy identities/offsets in the isolated candidate without remeshing."""
    children = list(root.children)
    occupied = {obj.get(PART_ID) for obj in root.children_recursive if obj.get(PART_ID)}
    if len(occupied) != sum(bool(obj.get(PART_ID)) for obj in root.children_recursive):
        raise BlenderError("Duplicate part ids in target geometry")
    if item["kind"] == "compound":
        specs = item["parts"]
        candidates = []
        for index, spec in enumerate(specs):
            identifier = _part_id(spec, index)
            matches = [child for child in children if child.get(PART_ID) == identifier]
            if not matches:
                matches = [child for child in children if not child.get(PART_ID) and re.search(r"/part " + str(index + 1) + r"(?:\.\d+)?$", child.name)]
            if len(matches) != 1:
                raise BlenderError(f"Cannot resolve saved part {identifier}; refusing template reconstruction")
            child = matches[0]
            child[PART_ID] = identifier
            child[PART_ROLE] = spec.get("role", child.get(PART_ROLE, ""))
            occupied.add(identifier)
            # v1 primitive positions described the unrotated bottom center.
            raw_center = authored_location(spec)
            candidates.append(raw_center - child.location)
        if ORIGIN not in root:
            if not candidates:
                raise BlenderError("Cannot infer the saved compound coordinate frame")
            # A manually displaced legacy part must not move the entire coordinate frame.
            groups = {}
            for offset in candidates:
                key = tuple(round(v, 5) for v in offset)
                groups.setdefault(key, []).append(offset)
            offsets = max(groups.values(), key=len)
            if len(groups) > 1 and len(offsets) * 2 <= len(candidates):
                raise BlenderError("Ambiguous legacy part origin; no geometry was changed")
            root[ORIGIN] = list(offsets[0])
    elif ORIGIN not in root:
        root[ORIGIN] = [0., 0., 0.]
    # Extra real parts (including user-authored detail) remain present and identifiable.
    for index, child in enumerate(root.children_recursive):
        if PART_ID not in child:
            identifier = f"extra_{index + 1:03d}"
            while identifier in occupied:
                identifier += "_"
            child[PART_ID] = identifier
            child[PART_ROLE] = child.name.rsplit("/", 1)[-1]
            occupied.add(identifier)
    return Vector(root[ORIGIN])


def authored_location(spec):
    position = Vector(spec.get("position", [0, 0, 0]))
    if spec["shape"] in {"box", "cylinder", "sphere", "cone"}:
        position.z += spec["dimensions"][2] / 2
    return position


def _parts(root):
    result = {}
    for obj in root.children_recursive:
        identifier = obj.get(PART_ID)
        if identifier:
            if identifier in result:
                raise BlenderError("Duplicate part ids in target geometry")
            result[identifier] = obj
    return result


def geometry_bounds(root, depsgraph=None):
    """Actual evaluated geometry bounds in the persistent root local frame."""
    bpy.context.view_layer.update()
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    inverse = root.matrix_world.inverted_safe()
    points = []
    for obj in root.children_recursive:
        if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        points.extend(inverse @ evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    if not points or not all(math.isfinite(value) for point in points for value in point):
        raise BlenderError("Target has no finite evaluated geometry bounds")
    return [Vector(tuple(min(point[i] for point in points) for i in range(3))), Vector(tuple(max(point[i] for point in points) for i in range(3)))]


def _delete_members(scene, root, members):
    members = list(members)
    _check_owned(scene, root, members)
    for obj in reversed(members):
        bpy.data.objects.remove(obj, do_unlink=True)


def _metadata(root, item):
    root.name = item["name"]
    root["sceneloop_kind"] = item["kind"]
    root["key_object"] = item["key_object"] if "key_object" in item else False
    root["requested_features"] = json.dumps(item.get("features", []), ensure_ascii=False)


ABS_TOL = 1e-5
REL_TOL = 1e-5
# Non-uniform scale makes a correct XYZ euler decompose about 4e-4 rad off.
# 1e-3 rad is 0.057 degrees: it accepts that single-precision residue and still
# rejects a one-degree miss (about 0.017 rad).
ROT_TOL = 1e-3


def _component_close(actual, expected):
    return abs(float(actual) - float(expected)) <= ABS_TOL + REL_TOL * max(abs(float(actual)), abs(float(expected)))


def _rotation_close(actual, expected):
    delta = actual.transposed() @ expected
    trace = float(delta[0][0] + delta[1][1] + delta[2][2])
    cosine = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.acos(cosine) <= ROT_TOL


def _evaluated_world(root):
    bpy.context.view_layer.update()
    return root.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()


def _mesh_extent(root):
    """Unscaled parent-space size of the current mesh children."""
    corners = [child.matrix_local @ Vector(corner)
               for child in root.children_recursive if child.type == "MESH"
               for corner in child.bound_box]
    if not corners:
        raise BlenderError("历史对象的缩放坐标系无法可靠解释，拒绝替换 kind")
    low = [min(point[i] for point in corners) for i in range(3)]
    high = [max(point[i] for point in corners) for i in range(3)]
    return [high[i] - low[i] for i in range(3)]


def _explainable_transform(matrix):
    location, rotation, scale = matrix.decompose()
    rebuilt = Matrix.LocRotScale(location, rotation, scale)
    return all(_component_close(rebuilt[row][col], matrix[row][col]) for row in range(4) for col in range(4))


def _scale_basis(root, old, make_object):
    """Root scale per unit API multiplier, independent of later part extents.

    Legacy fixed templates can be explained only when their original geometry
    still matches. A legacy compound's current parts cannot reveal its creation
    envelope, so missing provenance is rejected rather than guessed.
    """
    if SCALE_BASIS not in root:
        if old["kind"] == "compound":
            raise BlenderError("历史 compound 缺少创建/尺寸归一化依据，拒绝继承 kind 缩放；请显式指定 scale")
        reference = make_object(old)
        try:
            actual = {child.get(PART_ID): child for child in root.children}
            canonical = {child.get(PART_ID): child for child in reference.children}
            matches = (len(actual) == len(root.children) and None not in actual
                       and set(actual) == set(canonical))
            for identifier, child in actual.items():
                template = canonical.get(identifier)
                if not matches or template is None:
                    matches = False
                    break
                if (child.type != "MESH" or child.children or child.modifiers
                        or child.data.shape_keys
                        or len(child.data.vertices) != len(template.data.vertices)
                        or [tuple(face.vertices) for face in child.data.polygons]
                        != [tuple(face.vertices) for face in template.data.polygons]
                        or any(not _component_close(child.matrix_local[r][c], template.matrix_local[r][c])
                               for r in range(4) for c in range(4))
                        or any(not _component_close(a.co[i], b.co[i])
                               for a, b in zip(child.data.vertices, template.data.vertices) for i in range(3))):
                    matches = False
                    break
            if not matches:
                raise BlenderError("历史模板几何与原模板不符且缺少归一化依据，拒绝继承 kind 缩放；请显式指定 scale")
            root[SCALE_BASIS] = list(reference[SCALE_BASIS])
        finally:
            for member in reversed(_members(reference)):
                bpy.data.objects.remove(member, do_unlink=True)
    try:
        basis = tuple(float(value) for value in root[SCALE_BASIS])
    except (TypeError, ValueError):
        basis = ()
    if len(basis) != 3 or any(not math.isfinite(value) or value <= 0 for value in basis):
        raise BlenderError("对象归一化依据无效，拒绝继承 kind 缩放")
    return basis


def _resolve_kind_transform(root, old, new, explicit, make_object):
    """Fix the target from the current evaluated root and this operation's keys.

    The replacement object's unevaluated matrix is not a target. API scale stays
    a user multiplier; the Blender root scale is derived after the new shape exists.
    """
    matrix = _evaluated_world(root)
    if not _explainable_transform(matrix):
        raise BlenderError("历史对象的缩放坐标系无法可靠解释，拒绝替换 kind")
    location, rotation, actual_scale = matrix.decompose()
    if "position" in explicit:
        expected_location = Vector(new["position"])
        plan_position = [float(value) for value in new["position"]]
    else:
        expected_location = Vector(location)
        plan_position = [float(location[i]) for i in range(3)]
    if "rotation" in explicit:
        euler = Euler(tuple(math.radians(value) for value in new["rotation"]), "XYZ")
        plan_rotation = [float(value) for value in new["rotation"]]
    else:
        euler = rotation.to_euler("XYZ")
        plan_rotation = [float(math.degrees(euler[i])) for i in range(3)]
    if "scale" in explicit:
        api_scale = tuple(float(value) for value in new["scale"])
    else:
        basis = _scale_basis(root, old, make_object)
        api_scale = []
        for index in range(3):
            if actual_scale[index] <= 0:
                raise BlenderError("历史对象的缩放坐标系无法可靠解释，拒绝替换 kind")
            value = float(actual_scale[index]) / basis[index]
            if not math.isfinite(value) or value <= 0:
                raise BlenderError("历史对象的缩放坐标系无法可靠解释，拒绝替换 kind")
            api_scale.append(value)
        api_scale = tuple(api_scale)
    return {"location": expected_location, "euler": euler, "api_scale": api_scale,
            "plan": {"position": plan_position, "rotation": plan_rotation, "scale": [float(value) for value in api_scale]}}


def _transfer_kind_shape(scene, root, replacement, resolved, dimensions):
    _delete_members(scene, root, list(root.children_recursive))
    for child in list(replacement.children):
        local = child.matrix_basis.copy()
        child.parent = root
        child.matrix_parent_inverse.identity()
        child.matrix_basis = local
    root[ORIGIN] = list(replacement.get(ORIGIN, [0, 0, 0]))
    bpy.data.objects.remove(replacement, do_unlink=True)
    bpy.context.view_layer.update()
    extent = _mesh_extent(root)
    scale = []
    for index in range(3):
        if extent[index] <= 1e-8:
            raise BlenderError("替换形体没有可测量的局部尺寸，拒绝提交")
        scale.append(float(dimensions[index]) * resolved["api_scale"][index] / extent[index])
    root[SCALE_BASIS] = [float(dimensions[i]) / extent[i] for i in range(3)]
    root.rotation_mode = "XYZ"
    root.location = resolved["location"]
    root.rotation_euler = tuple(resolved["euler"])
    root.scale = tuple(scale)
    bpy.context.view_layer.update()
    return tuple(scale)


def _sync_kind_expectation(expected, old, new, changed, dimension_scale=None):
    """Move an already applied kind target forward with a later explicit edit."""
    if "position" in changed:
        expected["location"] = Vector(new["position"])
    if "rotation" in changed:
        euler = Euler(tuple(math.radians(value) for value in new["rotation"]), "XYZ")
        expected["rotation_matrix"] = euler.to_matrix()
    if dimension_scale is not None:
        expected["scale"] = tuple(dimension_scale)
    elif "scale" in changed:
        expected["scale"] = tuple(expected["scale"][index] * float(new["scale"][index]) / float(old["scale"][index])
                                  for index in range(3))


def verify_kind_transforms(scene, expectations):
    """Compare evaluated world transforms with targets fixed before commit."""
    if not expectations:
        return
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    roots = _roots(scene)
    for identifier, expected in expectations.items():
        root = roots.get(identifier)
        if root is None:
            raise BlenderError(f"Kind replacement for {identifier} lost its semantic root before commit")
        location, rotation, scale = root.evaluated_get(depsgraph).matrix_world.decompose()
        if any(not _component_close(location[index], expected["location"][index]) for index in range(3)):
            raise BlenderError(f"Kind replacement for {identifier} did not land on the expected position")
        if not _rotation_close(rotation.to_matrix(), expected["rotation_matrix"]):
            raise BlenderError(f"Kind replacement for {identifier} did not land on the expected rotation")
        if any(not _component_close(scale[index], expected["scale"][index]) for index in range(3)):
            raise BlenderError(f"Kind replacement for {identifier} did not land on the expected scale")


def _apply_transform(root, old, new, changed):
    if "position" in changed:
        root.location = new["position"]
    if "rotation" in changed:
        root.rotation_mode = "XYZ"
        root.rotation_euler = tuple(math.radians(value) for value in new["rotation"])
    if "dimensions" in changed:
        lo, hi = geometry_bounds(root)
        extents = hi - lo
        if any(value <= 1e-8 for value in extents):
            raise BlenderError("Cannot dimension an asset with a zero-sized local extent")
        root[SCALE_BASIS] = [float(new["dimensions"][i]) / extents[i] for i in range(3)]
        root.scale = tuple(root[SCALE_BASIS][i] * new["scale"][i] for i in range(3))
    elif "scale" in changed:
        root.scale = tuple(root.scale[i] * new["scale"][i] / old["scale"][i] for i in range(3))


def _update_parts(scene, root, old, new, make_part):
    offset = ensure_part_metadata(root, old)
    actual = _parts(root)
    before = {_part_id(spec, i): spec for i, spec in enumerate(old["parts"])}
    after = {_part_id(spec, i): spec for i, spec in enumerate(new["parts"])}
    for identifier, spec in before.items():
        if identifier not in actual:
            raise BlenderError(f"Saved part {identifier} is missing")
        if identifier not in after:
            obj = actual[identifier]
            _delete_members(scene, root, [obj, *obj.children_recursive])
    for identifier, spec in after.items():
        previous = before.get(identifier)
        if previous == spec:
            continue
        obj = actual.get(identifier)
        if previous is None and obj is not None:
            raise BlenderError(f"Part id {identifier} is already used by saved unplanned geometry")
        changed = set(spec) | set(previous or {})
        changed = {key for key in changed if (previous or {}).get(key) != spec.get(key)}
        if previous is not None and changed <= {"role", "position", "rotation", "dimensions"}:
            # Preserve actual Mesh, custom modifiers/materials and non-planned detail.
            _check_owned(scene, root, [obj])
            if "position" in changed or "dimensions" in changed:
                obj.location = authored_location(spec) - offset
            if "dimensions" in changed:
                obj.scale = tuple(obj.scale[i] * spec["dimensions"][i] / previous["dimensions"][i] for i in range(3))
            if "rotation" in changed:
                obj.rotation_mode = "XYZ"
                obj.rotation_euler = tuple(math.radians(value) for value in spec["rotation"])
            obj[PART_ROLE] = spec.get("role", "")
            continue
        if previous is not None and obj is not None and part_has_derived(obj):
            blocked = sorted(changed - {"role", "position", "rotation", "dimensions"})
            if blocked:
                raise BlenderError(
                    f"Part {identifier} has host-verified derived cut geometry; replacing its mesh or shape parameters "
                    f"({', '.join(blocked)}) is refused. Transform it, cut it again, or remove it explicitly.")
        # Explicit shape/parameter edits replace only that named part's geometry.
        # Keep its Object identity and material/modifier stack, and reject dependent children.
        if obj is not None and previous is not None:
            _check_owned(scene, root, [obj])
            if obj.children:
                raise BlenderError("Cannot replace part geometry that owns child objects")
            if obj.data and getattr(obj.data, "shape_keys", None):
                raise BlenderError("Replacing geometry with shape keys requires an explicit supported operation")
        created = make_part(root, spec)
        created.location -= offset
        if obj is None or previous is None:
            continue
        if obj.type != created.type:
            bpy.data.objects.remove(created, do_unlink=True)
            raise BlenderError("Changing an existing part data type requires explicit removal and addition")
        materials = list(obj.data.materials)
        data = created.data
        obj.data = data
        obj.matrix_basis = created.matrix_basis.copy()
        obj[PART_ROLE] = spec.get("role", "")
        if materials:
            obj.data.materials.clear()
            for material in materials:
                obj.data.materials.append(material)
        bpy.data.objects.remove(created, do_unlink=True)


def apply_incremental_edit(scene, plan, edit_plan, make_object, make_part):
    """Apply validated operations to a frozen actual scene; caller commits only on success."""
    final_plan = apply_edit_plan(plan, edit_plan)
    current = plan
    affected = set()
    inherited = {}
    expectations = {}
    roots = _roots(scene)
    if set(roots) != {item["id"] for item in plan["objects"]}:
        raise BlenderError("Saved scene identities do not match its plan")
    for root in roots.values():
        if root.parent is not None:
            raise BlenderError("Managed semantic root has an unsupported parent")
    for operation in edit_plan["operations"]:
        next_plan = apply_edit_plan(current, {"operations": [operation]})
        old_items = {item["id"]: item for item in current["objects"]}
        new_items = {item["id"]: item for item in next_plan["objects"]}
        op = operation["op"]
        if op == "add":
            item = new_items[operation["object"]["id"]]
            roots[item["id"]] = make_object(item)
            affected.add(item["id"])
        else:
            identifier = operation["id"]
            affected.add(identifier)
            root = roots[identifier]
            _check_owned(scene, root)
            if op == "cut_box":
                cut_target(scene, root, operation["part_id"], operation)
            elif op == "remove":
                _delete_members(scene, root, _members(root))
                del roots[identifier]
                inherited.pop(identifier, None)
                expectations.pop(identifier, None)
            else:
                old, new = old_items[identifier], new_items[identifier]
                if operation["op"] == "update" and "parts" in operation["changes"] and has_derived_geometry(root):
                    # Even an identical rewrite is refused: it claims whole-assembly authority.
                    raise BlenderError(
                        f"Object {identifier} owns host-verified derived cut geometry; a full parts replacement "
                        "would discard the actual mesh. Use add_part/update_part/remove_part for local changes.")
                explicit = set(operation.get("changes") or ())
                changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
                if "kind" in changed:
                    if has_derived_geometry(root):
                        raise BlenderError(
                            f"Object {identifier} owns host-verified derived cut geometry; replacing its kind "
                            "would discard the actual mesh. Remove those parts explicitly or keep editing them.")
                    # Resolve from the live root and this request's keys before the new shape exists.
                    resolved = _resolve_kind_transform(root, old, new, explicit, make_object)
                    inherited[identifier] = dict(inherited.get(identifier) or {})
                    for field, value in resolved["plan"].items():
                        if field in explicit:
                            inherited[identifier].pop(field, None)
                        else:
                            new[field] = value
                            inherited[identifier][field] = value
                    replacement = make_object(new)
                    scale = _transfer_kind_shape(scene, root, replacement, resolved, new["dimensions"])
                    expectations[identifier] = {
                        "location": Vector(resolved["location"]),
                        "rotation_matrix": resolved["euler"].to_matrix(),
                        "scale": scale,
                    }
                else:
                    for field in ("position", "rotation", "scale"):
                        if field in explicit and identifier in inherited:
                            inherited[identifier].pop(field, None)
                    if "parts" in changed:
                        _update_parts(scene, root, old, new, make_part)
                    _apply_transform(root, old, new, changed)
                    if identifier in expectations:
                        dimension_scale = None
                        if "dimensions" in changed:
                            low, high = geometry_bounds(root)
                            extents = high - low
                            dimension_scale = tuple(new["dimensions"][index] * new["scale"][index] / extents[index]
                                                    for index in range(3))
                        _sync_kind_expectation(expectations[identifier], old, new, changed, dimension_scale)
                _metadata(root, new)
        current = next_plan
    patched = copy.deepcopy(final_plan)
    indexed = {item["id"]: item for item in patched["objects"]}
    for identifier, fields in inherited.items():
        if fields and identifier in indexed:
            indexed[identifier].update(fields)
    executed = validate_plan(current)
    if executed != validate_plan(patched):
        raise BlenderError("Incremental operation result diverged from validated plan")
    # Plan envelopes are nominal after incremental detail. Check actual changed roots
    # before committing, since initial normalization factors can amplify a new part.
    for identifier in affected & roots.keys():
        root = roots[identifier]
        lo, hi = geometry_bounds(root)
        extents = [(hi[i] - lo[i]) * abs(root.scale[i]) for i in range(3)]
        if any(not math.isfinite(value) or value > 100.00001 for value in extents):
            raise BlenderError("Edited actual geometry exceeds the finite 100 metre per-axis size limit")
    # Actual managed-scene cost, counted from current original mesh data of every
    # mesh part (the same representation the Plan budget describes); cutting can
    # grow a mesh, so nominal Plan numbers are not used here.
    total_vertices = total_polygons = 0
    for root in roots.values():
        for obj in root.children_recursive:
            if obj.type != "MESH" or obj.data is None:
                continue
            total_vertices += len(obj.data.vertices)
            total_polygons += len(obj.data.polygons)
    if total_vertices > MAX_SCENE_VERTICES or total_polygons > MAX_SCENE_FACES:
        raise BlenderError("Edited actual scene geometry exceeds the 50000 vertices / 100000 faces generation limit")
    scene["sceneloop_plan"] = json.dumps(executed, ensure_ascii=False)
    bpy.context.view_layer.update()
    apply_incremental_edit.kind_expectations = expectations
    return executed
