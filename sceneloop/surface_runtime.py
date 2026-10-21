"""Trusted procedural surfaces/lights applied only to an isolated candidate."""
import json
import math

import bpy
from mathutils import Euler, Vector

from surface_contract import SurfaceError, canonical, digest, normalize
import surface_recipes

LIGHT_ID = "sceneloop_surface_light"


def _scalar(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SurfaceError("Nonfinite shader value")
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, set):
        return sorted(_scalar(v) for v in value)
    try:
        return [_scalar(v) for v in value]
    except TypeError:
        return None


def graph(tree):
    """Actual effective node fields/inputs/links, excluding editor coordinates."""
    if tree is None or len(tree.nodes) > 64 or tree.animation_data:
        raise SurfaceError("Managed shader graph missing or exceeds node limit")
    rows = []
    for n in tree.nodes:
        props = {}
        for p in n.bl_rna.properties:
            if p.identifier in {"rna_type", "name", "label", "location", "location_absolute", "width", "height", "dimensions", "select", "show_options", "show_preview", "show_texture", "hide", "color", "use_custom_color", "parent"} or p.type in {"POINTER", "COLLECTION"} or p.is_readonly:
                continue
            props[p.identifier] = _scalar(getattr(n, p.identifier))
        row = {"name": n.name, "type": n.bl_idname, "properties": props,
               "inputs": [{"index": i, "identifier": s.identifier, "value": _scalar(getattr(s, "default_value", None))} for i,s in enumerate(n.inputs)]}
        if hasattr(n, "color_ramp"):
            ramp = n.color_ramp
            row["ramp"] = {"interpolation": ramp.interpolation, "color_mode": ramp.color_mode,
                           "elements": [[_scalar(e.position), _scalar(e.color)] for e in ramp.elements]}
        rows.append(row)
    return {"nodes": sorted(rows, key=lambda r:r["name"]),
            "links": sorted([l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier] for l in tree.links)}


def _targets(scene, target):
    if "object_id" in target:
        roots = [o for o in scene.objects if o.get("sceneloop_id") == target["object_id"]]
        if len(roots) != 1:
            raise SurfaceError("Managed surface root missing/duplicate")
        objects = [o for o in roots[0].children_recursive if o.type == "MESH"]
        if "part_id" in target:
            objects = [o for o in objects if o.get("sceneloop_part_id") == target["part_id"]]
    else:
        objects = [o for o in scene.objects if o.type == "MESH" and o.get("sceneloop_asset_instance") == target["instance_id"]]
        if "node_id" in target:
            objects = [o for o in objects if o.get("sceneloop_asset_node") == target["node_id"]]
    if not objects:
        raise SurfaceError("Surface target has no real mesh")
    if "part_id" in target or "node_id" in target:
        if len(objects) != 1:
            raise SurfaceError("Surface part/node identity is not unique")
    return objects


def _bindings(scene, state):
    # Broad object defaults first; explicit part/node bindings override that slot.
    rows = sorted(state["assignments"], key=lambda r:(bool("part_id" in r["target"] or "node_id" in r["target"]), canonical(r["target"])))
    bindings = {}
    for row in rows:
        for obj in _targets(scene, row["target"]):
            slot = row["target"]["slot"]
            if slot >= len(obj.material_slots) and not (slot == 0 and not obj.material_slots):
                raise SurfaceError("Target material slot does not exist")
            bindings[(obj.as_pointer(), slot)] = (obj, row)
    return list(bindings.values())


def apply_materials(scene, plan):
    if "surface" not in plan:
        return
    state = normalize(plan["surface"], plan)
    bindings = _bindings(scene, state)
    for obj, row in bindings:
        slot = row["target"]["slot"]
        mat = surface_recipes.material(row["surface"])
        if not obj.material_slots:
            # Adding an empty slot is a data operation: detach shared data first.
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.data.materials.append(mat)
        else:
            # OBJECT binding prevents changing unrelated users of a shared Mesh.
            obj.material_slots[slot].link = "OBJECT"
            obj.material_slots[slot].material = mat


def _light_pose(row):
    if row["type"] == "SUN":
        return Euler([math.radians(v) for v in row["rotation"]], "XYZ").to_matrix().to_4x4()
    matrix = ((Vector(row["target"]) - Vector(row["position"])).to_track_quat("-Z", "Y").to_matrix().to_4x4()
              if row["type"] == "AREA" else Euler((0,0,0)).to_matrix().to_4x4())
    matrix.translation = Vector(row["position"])
    return matrix


def _light_data(row):
    data = bpy.data.lights.new("SceneLoop " + row["id"], row["type"])
    # Blender 5.2 lights use an Emission -> Light Output graph by default.
    # Author the finite light and later compare that actual graph too.
    data.use_nodes = True
    data.energy = row["energy"]; data.color = row["color"]
    if row["type"] == "SUN":
        data.angle = math.radians(row["angle"])
    elif row["type"] == "AREA":
        data.shape = "DISK"; data.size = row["size"]
    else:
        data.shadow_soft_size = row["size"]
    return data


def apply_lighting(scene, spec):
    scene.world = surface_recipes.world(spec["world"])
    for obj in list(scene.objects):
        if LIGHT_ID in obj:
            # This scene is isolated; unlink only here, never delete other scenes' light objects.
            for collection in list(obj.users_collection):
                if collection == scene.collection or collection in scene.collection.children_recursive:
                    collection.objects.unlink(obj)
    for row in spec["lights"]:
        data = _light_data(row)
        obj = bpy.data.objects.new(data.name, data); obj[LIGHT_ID] = row["id"]
        scene.collection.objects.link(obj); obj.matrix_world = _light_pose(row)
    bpy.context.view_layer.update()


def apply_initial(scene, plan):
    apply_materials(scene, plan)
    if "lighting" in plan.get("surface", {}):
        apply_lighting(scene, plan["surface"]["lighting"])


def inspect(scene, plan):
    if "surface" not in plan:
        return None
    state = normalize(plan["surface"], plan)
    records = {}; expected_graphs = {}
    for obj, row in _bindings(scene, state):
        key = canonical(row["surface"])
        if key not in expected_graphs:
            reference = surface_recipes.material(row["surface"])
            try:
                expected_graphs[key] = graph(reference.node_tree)
            finally:
                bpy.data.materials.remove(reference)
        slot = row["target"]["slot"]
        mat = obj.material_slots[slot].material if len(obj.material_slots) > slot else None
        actual = graph(mat.node_tree) if mat and mat.use_nodes else None
        if actual != expected_graphs[key]:
            raise SurfaceError("Actual shader does not match the declared supported surface: " + obj.name)
        record = records.setdefault(canonical(row["target"]), {"target": row["target"], "objects": []})
        record["objects"].append({"name": obj.name, "slot": slot, "material_name": mat.name, "graph_digest": digest(actual)})
    measured_lighting = None
    if "lighting" in state:
        spec = state["lighting"]
        reference = surface_recipes.world(spec["world"])
        try:
            expected_world = graph(reference.node_tree)
        finally:
            bpy.data.worlds.remove(reference)
        if not scene.world or not scene.world.use_nodes or graph(scene.world.node_tree) != expected_world:
            raise SurfaceError("Actual World does not match authored lighting")
        managed = [o for o in scene.objects if LIGHT_ID in o]
        if len(managed) != len(spec["lights"]) or {o[LIGHT_ID] for o in managed} != {r["id"] for r in spec["lights"]}:
            raise SurfaceError("Managed light identities do not match plan")
        rows = []
        for row in spec["lights"]:
            obj = next(o for o in managed if o[LIGHT_ID] == row["id"])
            if obj.type != "LIGHT" or obj.data.type != row["type"] or obj.parent or obj.constraints or obj.animation_data or obj.data.animation_data or obj.hide_render:
                raise SurfaceError("Managed light structure changed")
            reference_light = _light_data(row)
            try:
                expected_light_graph = graph(reference_light.node_tree)
            finally:
                bpy.data.lights.remove(reference_light)
            actual_light_graph = graph(obj.data.node_tree)
            if actual_light_graph != expected_light_graph:
                raise SurfaceError("Managed light nodes differ from plan")
            expected = _light_pose(row)
            if max(abs(a-b) for ar,br in zip(obj.matrix_world,expected) for a,b in zip(ar,br)) > 1e-5:
                raise SurfaceError("Managed light pose differs from plan")
            vals = [obj.data.energy, *obj.data.color]
            wanted = [row["energy"], *row["color"]]
            vals.append(obj.data.angle if row["type"] == "SUN" else obj.data.size if row["type"] == "AREA" else obj.data.shadow_soft_size)
            wanted.append(math.radians(row["angle"]) if row["type"] == "SUN" else row["size"])
            if any(abs(a-b) > 1e-5 * max(1,abs(b)) for a,b in zip(vals,wanted)) or (row["type"] == "AREA" and obj.data.shape != "DISK"):
                raise SurfaceError("Managed light values differ from plan")
            rows.append({"id": row["id"], "type": obj.data.type, "energy": _scalar(obj.data.energy),
                         "color": _scalar(obj.data.color), "matrix_world": [_scalar(v) for v in obj.matrix_world],
                         "graph_digest": digest(actual_light_graph)})
        measured_lighting = {"managed_lights": rows, "world_graph_digest": digest(expected_world),
                             "unmanaged_light_count": sum(o.type == "LIGHT" and LIGHT_ID not in o for o in scene.objects)}
    for row in records.values():
        row["objects"].sort(key=lambda r:(r["name"], r["slot"]))
    return {"schema_version": 1, "verified": True, "state": state, "state_digest": digest(state),
            "materials": [records[k] for k in sorted(records)], "lighting": measured_lighting,
            "scope": "Supported procedural nodes, bindings and authored lights verified; world-metre textures; shading bump only. Visual quality requires appearance images."}
