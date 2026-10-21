"""Blender-side static assets inside an isolated parametric candidate."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import uuid

import bpy
from mathutils import Euler, Matrix, Vector
from asset_contract import (AssetError, NODE_IMPORT_KEY, encode_glb, instance_id,
                            measured_rotation, parse_glb, registration, transform)

INSTANCE = "sceneloop_asset_instance"
ROOT = "sceneloop_asset_root"
NODE = "sceneloop_asset_node"


def check_scene_resources(scene, asset_objects=None):
    """External instance budgets and original parametric budgets stay separate."""
    from asset_contract import (MAX_VERTICES, MAX_TRIANGLES, MAX_SCENE_ASSET_VERTICES,
        MAX_SCENE_ASSET_TRIANGLES, MAX_PARAMETRIC_VERTICES, MAX_PARAMETRIC_TRIANGLES)
    if asset_objects is None:
        asset_objects = [obj for obj in scene.objects if obj.get(INSTANCE)]
    asset_objects = set(asset_objects)
    external = [obj for obj in scene.objects if obj.type == "MESH" and obj in asset_objects]
    parametric = [obj for obj in scene.objects if obj.type == "MESH" and obj not in asset_objects]
    def cost(objects):
        vertices = triangles = 0
        for obj in objects:
            obj.data.calc_loop_triangles()
            vertices += len(obj.data.vertices)
            triangles += len(obj.data.loop_triangles)
        return vertices,triangles
    groups = {}
    for obj in external:
        groups.setdefault(obj.get(INSTANCE),[]).append(obj)
    for objects in groups.values():
        v,t = cost(objects)
        if v > MAX_VERTICES or t > MAX_TRIANGLES:
            raise AssetError("External asset instance exceeds geometry budget")
    ev,et = cost(external)
    pv,pt = cost(parametric)
    if ev > MAX_SCENE_ASSET_VERTICES or et > MAX_SCENE_ASSET_TRIANGLES:
        raise AssetError("Scene external asset geometry exceeds budget")
    if pv > MAX_PARAMETRIC_VERTICES or pt > MAX_PARAMETRIC_TRIANGLES:
        raise AssetError("Parametric geometry exceeds original budget")
    return {"external":{"vertices":ev,"triangles":et},"parametric":{"vertices":pv,"triangles":pt}}


def _matrix(matrix):
    return [[float(v) for v in row] for row in matrix]


def _near(a, b):
    return max(abs(x-y) for ar, br in zip(a,b) for x,y in zip(ar,br)) <= 1e-5


def _pose(obj):
    return transform({"position":list(obj.location), "rotation":measured_rotation([math.degrees(v) for v in obj.rotation_euler]), "scale":list(obj.scale)})


def _pose_matrix(value):
    return Matrix.LocRotScale(Vector(value["position"]), Euler([math.radians(v) for v in value["rotation"]], "XYZ").to_quaternion(), Vector(value["scale"]))


def inspect_assets(scene, plan):
    extension = plan.get("assets", {"schema_version":1,"instances":[]})
    registration(extension, [item["id"] for item in plan["objects"]])
    marked = [obj for obj in scene.objects if any(key in obj for key in (INSTANCE,ROOT,NODE))]
    if len(marked) != sum(1+len(row["nodes"]) for row in extension["instances"]):
        raise AssetError("Actual asset objects do not match Scene registration")
    bpy.context.view_layer.update(); result = []
    for row in extension["instances"]:
        members = [obj for obj in marked if obj.get(INSTANCE) == row["instance_id"]]
        roots = [obj for obj in members if obj.get(ROOT) == row["root_id"]]
        if len(roots) != 1 or len(members) != 1+len(row["nodes"]):
            raise AssetError("Missing/duplicate asset root or node identity")
        root = roots[0]
        if (root.type != "EMPTY" or root.parent is not None or root.rotation_mode != "XYZ"
                or NODE in root or "sceneloop_id" in root
                or not _near(_matrix(root.matrix_world), _matrix(_pose_matrix(row["transform"])))):
            raise AssetError("Asset root identity/actual transform mismatch")
        by_id = {}
        for obj in members:
            if obj == root:
                continue
            key = obj.get(NODE)
            if key in by_id or ROOT in obj or "sceneloop_id" in obj or "sceneloop_part_id" in obj:
                raise AssetError("Asset node cannot claim parametric identity")
            by_id[key] = obj
        if set(by_id) != {node["node_id"] for node in row["nodes"]}:
            raise AssetError("Asset node registration mismatch")
        points = []; mesh_count = vertices = polygons = 0; measured = []
        for node in row["nodes"]:
            obj = by_id[node["node_id"]]
            parent = root if node["parent_id"] is None else by_id[node["parent_id"]]
            if (obj.type != node["type"] or obj.parent != parent
                    or not _near(_matrix(obj.matrix_local), node["matrix_local"])
                    or obj.animation_data or obj.constraints or obj.modifiers
                    or (obj.type == "MESH" and obj.data.shape_keys)):
                raise AssetError("Asset hierarchy/local transform or static content changed")
            if obj.type == "MESH":
                if any(not math.isfinite(v) for vertex in obj.data.vertices for v in vertex.co):
                    raise AssetError("Nonfinite imported asset vertex")
                points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
                mesh_count += 1; vertices += len(obj.data.vertices); polygons += len(obj.data.polygons)
            measured.append({"node_id":node["node_id"],"source_index":node["source_index"],"parent_id":node["parent_id"],
                             "type":obj.type,"matrix_world":_matrix(obj.matrix_world)})
        if not points or any(not math.isfinite(v) for point in points for v in point):
            raise AssetError("Asset has no finite mesh bounds")
        result.append({"instance_id":row["instance_id"],"root_id":row["root_id"],"source":copy.deepcopy(row["source"]),
                       "transform":_pose(root),"matrix_world":_matrix(root.matrix_world),"nodes":measured,
                       "mesh_count":mesh_count,"vertices":vertices,"polygons":polygons,
                       "bounds":[[min(p[i] for p in points) for i in range(3)],[max(p[i] for p in points) for i in range(3)]]})
    if result:
        count = [obj for obj in scene.objects if obj.type == "MESH"]
        check_scene_resources(scene)
    return result


def _load_glb(path):
    result = bpy.ops.import_scene.gltf(filepath=str(path), import_pack_images=True)
    if result != {"FINISHED"}:
        raise AssetError("Blender did not finish the GLB import")


def import_asset(scene, plan, path, source, identifier, pose):
    inspect_assets(scene, plan); identifier = instance_id(identifier); pose = transform(pose)
    registry = copy.deepcopy(plan.get("assets", {"schema_version":1,"instances":[]}))
    if identifier in {item["id"] for item in plan["objects"]} | {item["instance_id"] for item in registry["instances"]}:
        raise AssetError("Asset instance conflicts with an existing id")
    if len(registry["instances"]) >= 16:
        raise AssetError("Candidate asset limit exceeded")
    raw = Path(path).read_bytes()
    if source != {"sha256":hashlib.sha256(raw).hexdigest(),"byte_size":len(raw)}:
        raise AssetError("Fixed GLB source bytes changed before import")
    doc, binary = parse_glb(raw)
    identities = {index:uuid.uuid4().hex for index in range(len(doc["nodes"]))}
    for index, node in enumerate(doc["nodes"]):
        node.setdefault("extras", {})[NODE_IMPORT_KEY] = index
    temporary = Path(path).parent / (".asset-decode-"+uuid.uuid4().hex+".glb")
    objects_before = {obj.as_pointer() for obj in bpy.data.objects}
    images_before = {item.as_pointer() for item in bpy.data.images}
    temporary.write_bytes(encode_glb(doc, binary))
    try:
        _load_glb(temporary)
        created = [obj for obj in bpy.data.objects if obj.as_pointer() not in objects_before]
        bpy.context.view_layer.update()
        if (len(created) != len(identities) or any(obj.type not in {"MESH","EMPTY"} or NODE_IMPORT_KEY not in obj for obj in created)):
            raise AssetError("Imported objects cannot be bound to the declared static nodes")
        by_index = {obj[NODE_IMPORT_KEY]:obj for obj in created}
        if set(by_index) != set(identities):
            raise AssetError("Imported node identities are missing or duplicate")
        # Keep importer-authored local transforms. Root attachment starts at identity.
        root_id = uuid.uuid4().hex
        root = bpy.data.objects.new("SceneLoop external asset", None); scene.collection.objects.link(root)
        root[INSTANCE], root[ROOT] = identifier, root_id
        nodes = []
        reverse = {obj:index for index,obj in by_index.items()}
        for index, obj in by_index.items():
            if obj.parent is not None and obj.parent not in reverse:
                raise AssetError("Imported hierarchy depends on a preexisting object")
            parent_index = reverse.get(obj.parent)
            if obj.parent is None:
                local = obj.matrix_world.copy(); obj.parent = root; obj.matrix_parent_inverse = Matrix.Identity(4); obj.matrix_basis = local
            obj[INSTANCE], obj[NODE] = identifier, identities[index]
            del obj[NODE_IMPORT_KEY]
            nodes.append({"node_id":identities[index],"source_index":index,"parent_id":identities[parent_index] if parent_index is not None else None,
                          "type":obj.type,"matrix_local":_matrix(obj.matrix_local)})
        root.rotation_mode = "XYZ"; root.location = pose["position"]
        root.rotation_euler = [math.radians(v) for v in pose["rotation"]]; root.scale = pose["scale"]
        bpy.context.view_layer.update()
        for image in bpy.data.images:
            if image.as_pointer() not in images_before:
                if image.packed_file is None:
                    image.pack()
                if image.packed_file is None:
                    raise AssetError("Imported image dependency was not packed")
        row = {"instance_id":identifier,"root_id":root_id,"source":copy.deepcopy(source),"transform":_pose(root),"nodes":nodes}
        registry["instances"].append(row); registration(registry,[item["id"] for item in plan["objects"]])
        plan = copy.deepcopy(plan); plan["assets"] = registry
        inspect_assets(scene, plan)
        return plan, row
    finally:
        temporary.unlink(missing_ok=True)


def transform_asset(scene, plan, identifier, changes):
    inspect_assets(scene, plan); identifier = instance_id(identifier); changes = transform(changes, partial=True)
    plan = copy.deepcopy(plan)
    row = next((item for item in plan.get("assets",{}).get("instances",[]) if item["instance_id"]==identifier), None)
    if row is None:
        raise AssetError("Asset instance not registered in this candidate")
    root = next(obj for obj in scene.objects if obj.get(ROOT)==row["root_id"])
    # Read untouched components from the current object, never stale Plan defaults.
    value = _pose(root); value.update(changes)
    root.location = value["position"]; root.rotation_euler = [math.radians(v) for v in value["rotation"]]; root.scale = value["scale"]
    bpy.context.view_layer.update(); row["transform"] = _pose(root)
    inspect_assets(scene, plan)
    return plan, row
