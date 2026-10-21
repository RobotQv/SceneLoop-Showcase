"""Blender-side implementation. Only validated data enters these modelling tools."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
import sys
import traceback
import uuid

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_adapter import (BlenderAdapter, BlenderError, apply_edit_plan, ground_warnings, observation_directory, replace_candidate_files, validate_plan, validate_view_spec, render_execution_slot)
import incremental_scene
from incremental_scene import (PART_ID, PART_ROLE, ORIGIN, apply_incremental_edit, ensure_part_metadata,
                                geometry_bounds)
from geometry_cut_runtime import DERIVED_KEY, verify_derived_geometry
from geometry_runtime import GeometryValidationError
from observation_runtime import aim_camera, observation_metadata, observation_mode, StructureObservationRefused
import asset_runtime
import surface_contract
import surface_runtime
from asset_contract import AssetError


def _activate(scene):
    if bpy.context.window:
        bpy.context.window.scene = scene


def _material():
    scene = bpy.context.scene
    material = bpy.data.materials.get(scene.get("sceneloop_material_name", ""))
    if material is not None:
        return material
    material = bpy.data.materials.new("SceneLoop neutral clay")
    scene["sceneloop_material_name"] = material.name
    material.diffuse_color = (.67, .69, .72, 1)
    material.use_nodes = True
    node = material.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (.67, .69, .72, 1)
    node.inputs["Roughness"].default_value = .82
    return material


def _shape(parent, shape, position, dimensions, rotation=(0, 0, 0), label="part"):
    location = (position[0], position[1], position[2] + dimensions[2] / 2)
    if shape == "box":
        bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    elif shape in ("cylinder", "cone"):
        bpy.ops.mesh.primitive_cone_add(vertices=16, radius1=.5, radius2=.5 if shape == "cylinder" else 0, depth=1, location=location)
    elif shape == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=.5, location=location)
    elif shape == "rock":
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=.5, location=location)
    else:
        raise BlenderError(f"未实现形状 {shape}")
    obj = bpy.context.object
    obj.name = f"{parent.name}/{label}"
    obj.parent = parent
    obj.location = location
    obj.dimensions = dimensions
    obj.rotation_euler = tuple(math.radians(x) for x in rotation)
    obj.data.materials.append(_material())
    return obj


def _create_part(parent, spec):
    """Create one authored part; the caller owns the persistent assembly frame."""
    if spec["shape"] in {"box", "cylinder", "sphere", "cone"}:
        obj = _shape(parent, spec["shape"], spec["position"], spec["dimensions"], spec["rotation"], spec.get("id", "part"))
    else:
        from geometry_runtime import create_part
        obj = create_part(parent, spec, material=_material(), label=spec.get("id", "part"))
    obj[PART_ID] = spec["id"]
    obj[PART_ROLE] = spec.get("role", "")
    return obj


def _prism(parent, *, base=.6, top=1, width=1.06, depth=1.06, label="gable"):
    vertices = [(-width/2, -depth/2, base), (width/2, -depth/2, base), (0, -depth/2, top), (-width/2, depth/2, base), (width/2, depth/2, base), (0, depth/2, top)]
    faces = [(0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)]
    mesh = bpy.data.meshes.new(f"{parent.name}/{label}")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = parent
    mesh.materials.append(_material())
    return obj


def _object(item):
    root = bpy.data.objects.new(item["name"], None)
    bpy.context.scene.collection.objects.link(root)
    root.empty_display_size = .2
    root["sceneloop_id"] = item["id"]
    root["sceneloop_kind"] = item["kind"]
    root["key_object"] = item.get("key_object", False)
    root["requested_features"] = json.dumps(item["features"], ensure_ascii=False)
    kind = item["kind"]

    def part(shape="box", p=(0, 0, 0), d=(1, 1, 1), r=(0, 0, 0), label="part"):
        return _shape(root, shape, p, d, r, label)

    if kind in ("ground", "wall", "box", "cylinder", "sphere"):
        part(shape=kind if kind in ("cylinder", "sphere") else "box", label=kind)
    elif kind == "house":
        part(d=(1, 1, .68), label="walls")
        _prism(root, base=.66)
        part(p=(0, -.513, 0), d=(.2, .04, .41), label="door")
        for x in (-.31, .31):
            part(p=(x, -.52, .34), d=(.17, .035, .19), label="window inset")
            part(p=(x, -.55, .425), d=(.2, .025, .025), label="window crossbar")
            part(p=(x, -.55, .34), d=(.02, .025, .19), label="window mullion")
        part(p=(.3, .28, .74), d=(.12, .12, .3), label="chimney")
    elif kind == "tent":
        _prism(root, base=0, width=1, depth=1, label="canvas")
        for x in (-.27, .27):
            part(p=(x, -.515, .02), d=(.045, .04, 1.05), r=(0, 27 if x < 0 else -27, 0), label="entrance seam")
        part(p=(0, -.54, .02), d=(.025, .03, .91), label="entrance split")
        part(p=(0, 0, .985), d=(.04, 1.13, .035), label="ridge pole")
    elif kind == "tree":
        part("cylinder", d=(.15, .15, .58), label="trunk")
        for z, diameter, height in ((.25, 1, .48), (.47, .78, .4), (.67, .52, .33)):
            part("cone", p=(0, 0, z), d=(diameter, diameter, height), label="canopy")
    elif kind == "rock":
        rock = part("rock", label="faceted rock")
        for vertex in rock.data.vertices:
            vertex.co.x *= 1 + .12 * math.sin(vertex.index * 2.3)
            vertex.co.y *= 1 + .15 * math.cos(vertex.index * 1.7)
    elif kind in ("table", "bench"):
        top = .9 if kind == "table" else .52
        for x in (-.4, .4):
            for y in (-.36, .36):
                part(p=(x, y, 0), d=(.09, .09, top), label="leg")
        part(p=(0, 0, top), d=(1, 1, .1), label="top")
        if kind == "bench":
            part(p=(0, .43, .76), d=(1, .1, .24), label="backrest")
            for x in (-.4, .4):
                part(p=(x, .43, .5), d=(.06, .06, .5), label="back support")
        else:
            for y in (-.36, .36):
                part(p=(0, y, .73), d=(.87, .055, .12), label="apron")
    elif kind == "crate":
        part(d=(.93, .93, .98), label="crate body")
        for x in (-.44, .44):
            for y in (-.485, .485):
                part(p=(x, y, 0), d=(.1, .05, 1), label="corner frame")
        for z in (.03, .86):
            for y in (-.49, .49):
                part(p=(0, y, z), d=(1, .06, .11), label="cross frame")
        part(p=(0, -.525, .065), d=(.1, .035, .98), r=(0, 43, 0), label="diagonal brace")
    elif kind == "barrel":
        part("cylinder", d=(.87, .87, 1), label="staves")
        part("sphere", p=(0, 0, .08), d=(1, 1, .83), label="barrel bulge")
        for z in (.12, .77):
            part("cylinder", p=(0, 0, z), d=(.97, .97, .075), label="hoop")
        part("cylinder", p=(0, 0, .965), d=(.8, .8, .045), label="lid")
    elif kind == "fence":
        for x in (-.46, 0, .46):
            part(p=(x, 0, 0), d=(.08, 1, 1), label="post")
        for z in (.31, .73):
            part(p=(0, -.08, z), d=(1, .65, .1), label="rail")
    elif kind == "campfire":
        for i in range(10):
            a = i * 2 * math.pi / 10
            part("rock", p=(.4 * math.cos(a), .4 * math.sin(a), 0), d=(.2, .2, .24), label="hearth stone")
        for a in (0, 60, 120):
            part("cylinder", p=(0, 0, .04), d=(.15, .15, .72), r=(90, 0, a), label="log")
        for x in (-.12, .12):
            part("cylinder", p=(x, 0, .18), d=(.06, .06, .62), r=(0, -20 if x > 0 else 20, 0), label="kindling")
    elif kind == "stairs":
        for i in range(6):
            part(p=(0, -.5 + (i+.5)/6, 0), d=(1, 1/6, (i+1)/6), label=f"step {i+1}")
    elif kind == "compound":
        for i, spec in enumerate(item["parts"]):
            _create_part(root, {"id": f"part_{i + 1:03d}", **spec})
    else:
        raise BlenderError(f"尚未实现物体类型 {kind}")

    # Normalize actual template bounds, so dimensions are the promised envelope.
    bpy.context.view_layer.update()
    corners = [child.matrix_local @ Vector(c) for child in root.children if child.type == "MESH" for c in child.bound_box]
    lo = Vector(tuple(min(p[i] for p in corners) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in corners) for i in range(3)))
    center = Vector(((lo.x+hi.x)/2, (lo.y+hi.y)/2, lo.z))
    root[ORIGIN] = list(center)
    for index, child in enumerate(root.children):
        child.location -= center
        if PART_ID not in child:
            child[PART_ID] = f"part_{index + 1:03d}"
            child[PART_ROLE] = child.name.rsplit("/", 1)[-1]
    root.location = item["position"]
    root.rotation_euler = tuple(math.radians(x) for x in item["rotation"])
    root[incremental_scene.SCALE_BASIS] = [item["dimensions"][i] / max(.0001, hi[i]-lo[i]) for i in range(3)]
    root.scale = tuple(root[incremental_scene.SCALE_BASIS][i] * item["scale"][i] for i in range(3))
    # The assigned TRS is not visible on matrix_world until the dependency graph updates.
    bpy.context.view_layer.update()
    return root


def _camera(scene, name, position, target, lens, roll_degrees=0):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = position
    aim_camera(obj, target, roll_degrees)
    data.lens = lens
    data.clip_end = 2000
    return obj


def create_scene(plan, workspace):
    plan = validate_plan(plan)
    name = f"SceneLoop candidate {workspace.name}"
    scene = bpy.data.scenes.new(name)
    _activate(scene)
    scene["sceneloop_workspace"] = str(workspace)
    scene["sceneloop_plan"] = json.dumps(plan, ensure_ascii=False)
    for item in plan["objects"]:
        _object(item)
    camera = plan["camera"]
    scene.camera = _camera(scene, "SceneLoop main", camera["position"], camera["target"], camera["lens"], camera.get("roll_degrees", 0))
    direction = Vector(camera["position"]) - Vector(camera["target"])
    auxiliary = Vector(camera["target"]) + Vector((-direction.y, direction.x, max(direction.z, direction.length*.4)))
    _camera(scene, "SceneLoop auxiliary", auxiliary, camera["target"], camera["lens"])
    world = bpy.data.worlds.new(f"{name} world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.65, .68, .74, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = .65
    scene.world = world
    data = bpy.data.lights.new("SceneLoop key", "SUN")
    data.energy = 2.5
    data.angle = math.radians(12)
    light = bpy.data.objects.new("SceneLoop key", data)
    light[surface_runtime.LIGHT_ID] = "__default__"
    scene.collection.objects.link(light)
    light.rotation_euler = (math.radians(24), math.radians(-20), math.radians(-35))
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 12
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 960
    scene.render.resolution_y = 640
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    if plan.get("render_profile") == "showcase_v1":
        scene.view_settings.view_transform = "AgX"
        scene.cycles.samples = 24
    surface_runtime.apply_initial(scene, plan)
    surface_runtime.inspect(scene, plan)
    bpy.context.view_layer.update()
    return scene


def inspect_scene(scene):
    """Inspect actual evaluated hierarchy, including detail absent from the original plan."""
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    plan = validate_plan(json.loads(scene["sceneloop_plan"]))
    planned = {item["id"]: item for item in plan["objects"]}
    rows = []
    for obj in scene.objects:
        if "sceneloop_id" not in obj:
            continue
        identifier = obj["sceneloop_id"]
        if identifier not in planned:
            raise BlenderError("Saved scene has an unregistered semantic object")
        ensure_part_metadata(obj, planned[identifier])
        parts, points = [], []
        for child in obj.children_recursive:
            if child.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
                continue
            evaluated = child.evaluated_get(depsgraph)
            corners = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
            if not corners or not all(math.isfinite(value) for point in corners for value in point):
                raise BlenderError("Part has invalid evaluated bounds")
            points.extend(corners)
            mesh = evaluated.to_mesh()
            try:
                vertices = len(mesh.vertices) if mesh else 0
                polygons = len(mesh.polygons) if mesh else 0
            finally:
                evaluated.to_mesh_clear()
            row = {"part_id": child[PART_ID], "role": child.get(PART_ROLE, ""), "name": child.name,
                   "type": child.type, "parent_part_id": child.parent.get(PART_ID) if child.parent != obj else None,
                   "bounds": [[min(point[i] for point in corners) for i in range(3)], [max(point[i] for point in corners) for i in range(3)]],
                   "position": list(child.location), "rotation": [math.degrees(value) for value in child.rotation_euler], "scale": list(child.scale),
                   "matrix_world": [list(row) for row in child.matrix_world], "vertices": vertices, "polygons": polygons,
                   "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in child.modifiers],
                   "materials": [slot.material.name if slot.material else None for slot in child.material_slots]}
            if DERIVED_KEY in child:
                # Host verification against the current original mesh; a stale or
                # malformed record stays visible as provenance, never as current fact.
                row["derived_geometry"] = verify_derived_geometry(child, object_id=identifier,
                                                                   part_id=child[PART_ID])
            parts.append(row)
        if not points:
            raise BlenderError("Semantic object has no inspectable geometry")
        bounds = [[min(point[i] for point in points) for i in range(3)], [max(point[i] for point in points) for i in range(3)]]
        local_bounds = geometry_bounds(obj, depsgraph)
        rows.append({"id": identifier, "name": obj.name, "kind": obj["sceneloop_kind"], "key_object": bool(obj["key_object"]),
                     "mesh_parts": len(parts), "bounds": bounds, "parts": parts,
                     "frame": {"origin_offset": list(obj[ORIGIN]), "matrix_world": [list(row) for row in obj.matrix_world],
                               "local_bounds": [list(point) for point in local_bounds], "geometry_edit_refits_existing_parts": False,
                               "part_coordinates": "authored coordinates minus persistent origin_offset; mesh/extrusion/tube use local origins, primitives use unrotated bottom centers"},
                     "shape_status": "parameterized_template" if obj["sceneloop_kind"] != "compound" else "explicit_parts",
                     "requested_features": json.loads(obj["requested_features"])})
    result = {"scene": scene.name, "semantic_objects": rows, "object_count": len(rows), "mesh_count": sum(o.type == "MESH" for o in scene.objects),
            "key_objects": [x["id"] for x in rows if x["key_object"]], "verification": "structural_only", "visual_acceptance": "requires_review",
            "limitations": ["部件来自实际evaluated几何；features是需求备注，不代表细节已实现", "包围盒是测量依据，不证明精确碰撞、支撑关系或可行走性", "未验证与参考图的视觉相似度或游戏引擎规格"],
            "warnings": ground_warnings(rows, plan), "cameras": [{"name": x.name, "position": list(x.location), "lens": x.data.lens} for x in scene.objects if x.type == "CAMERA"]}
    assets = asset_runtime.inspect_assets(scene, plan)
    if assets:
        result["assets"] = assets
    surfaces = surface_runtime.inspect(scene, plan)
    if surfaces is not None:
        result["surface"] = surfaces
    return result


def _load_library_scene(path, scene_name=None):
    with bpy.data.libraries.load(str(path), link=False) as (source, target):
        names = [name for name in source.scenes if name == scene_name] if scene_name else [name for name in source.scenes if name.startswith("SceneLoop candidate ")]
        if len(names) != 1:
            raise BlenderError("候选文件需要唯一的 SceneLoop 场景")
        target.scenes = names
    return target.scenes[0]


def _candidate(workspace, live, expected_workspace=None, frozen=False):
    expected = str(expected_workspace or workspace)
    if live and frozen:
        # A displayed candidate can have uncommitted user edits. Agent evidence
        # and forks must use the registered file, not those mutable objects.
        scene = _load_library_scene(workspace / "scene.blend")
        scene["sceneloop_frozen_input"] = True
        if scene.get("sceneloop_workspace") != expected:
            raise BlenderError("落盘候选不属于此 workspace")
        _activate(scene)
        return scene
    if not live:
        bpy.ops.wm.open_mainfile(filepath=str(workspace / "scene.blend"), load_ui=False, use_scripts=False)
    # Snapshot/rollback retain their displayed-scene semantics. Prefer the
    # current candidate when prior user-edited displays share its workspace.
    current = bpy.context.scene
    if current.get("sceneloop_workspace") == expected:
        return current
    for scene in bpy.data.scenes:
        if scene.get("sceneloop_workspace") == expected:
            _activate(scene)
            return scene
    if live and (workspace / "scene.blend").is_file():
        scene = _load_library_scene(workspace / "scene.blend")
        if scene.get("sceneloop_workspace") == expected:
            _activate(scene)
            return scene
        bpy.data.scenes.remove(scene)
    raise BlenderError("找不到此目录的 SceneLoop 候选场景")


def _consistent_plan(scene, workspace):
    plan = validate_plan(json.loads((workspace / "plan.json").read_text(encoding="utf-8")))
    if plan != validate_plan(json.loads(scene.get("sceneloop_plan", "{}"))):
        raise BlenderError("候选场景与 plan.json 不一致，拒绝继续编辑")
    asset_runtime.inspect_assets(scene, plan)
    surface_runtime.inspect(scene, plan)
    return plan


def _save_candidate(scene, workspace, live):
    target = workspace / "scene.blend"
    if live:
        # Only this candidate is exported. The user's open file is never saved.
        bpy.data.libraries.write(str(target), {scene}, fake_user=True)
        adapter = BlenderAdapter(blender_path=bpy.app.binary_path)
        adapter._run("finalize", workspace, scene_workspace=scene["sceneloop_workspace"])
    else:
        for other in list(bpy.data.scenes):
            if other != scene:
                bpy.data.scenes.remove(other)
        _activate(scene)
        bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)


def _commit_scene(scene, workspace, live, plan, names=None, *, preserve_failed=False):
    if "sceneloop_frozen_input" in scene:
        del scene["sceneloop_frozen_input"]
    staged = workspace / f".candidate-{uuid.uuid4().hex}"
    committed = False
    try:
        staged.mkdir()
        inspection = inspect_scene(scene)
        (staged / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (staged / "inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_candidate(scene, staged, live)
        replace_candidate_files(workspace, staged, names)
        committed = True
    except Exception as error:
        # A failure inside the commit window may leave a partial or complete
        # candidate whose result cannot be confirmed; it must never be reported
        # as a correctable parameter rejection.
        try:
            error.outcome = "unknown"
            error.outcome_unknown = True
        except Exception:
            raise BlenderError(str(error), outcome="unknown") from None
        if preserve_failed:
            failure = BlenderError(f"{error}; unknown asset candidate retained at {workspace} and {staged}", outcome="unknown")
            failure.pending_candidate = {"workspace":str(workspace),"staged_directory":str(staged)}
            raise failure from error
        raise
    finally:
        if committed or not preserve_failed:
            shutil.rmtree(staged, ignore_errors=True)
    return {"scene_path": str(workspace / "scene.blend"), "plan_path": str(workspace / "plan.json"), "inspection_path": str(workspace / "inspection.json"), "inspection": inspection, "workspace": str(workspace), "mode": "live" if live else "headless", "render_stale": True}


def _observe(scene, workspace, view_spec, output_id):
    view = validate_view_spec(view_spec)
    output = observation_directory(workspace, output_id)
    output.mkdir(parents=True, exist_ok=False)
    old_camera, old_filepath = scene.camera, scene.render.filepath
    temporary_camera = None
    try:
        if "preset" in view:
            prefix = "SceneLoop " + view["preset"]
            camera = next((obj for obj in scene.objects if obj.type == "CAMERA" and obj.name.startswith(prefix)), None)
            if camera is None:
                raise BlenderError("候选缺少要求的比较相机")
            target = json.loads(scene["sceneloop_plan"])["camera"]["target"]
        else:
            temporary_camera = _camera(scene, "SceneLoop observation", view["position"], view["target"], view["lens"], view.get("roll_degrees", 0))
            camera = temporary_camera
            target = view["target"]
        scene.camera = camera
        scene.render.filepath = str(output / "image.png")
        with observation_mode(scene, camera, target, view.get("mode", "appearance")) as profile:
            with render_execution_slot(scene.render.engine, getattr(scene.cycles, "device", None)):
                bpy.ops.render.render(write_still=True, scene=scene.name)
            metadata = {"workspace": str(workspace), "output_id": output_id, "view_spec": view, **observation_metadata(scene, camera, target, profile=profile), "comparison_camera_unchanged": True}
            metadata_path = output / "metadata.json"
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            return {**metadata, "image_path": str(output / "image.png"), "path": str(output / "image.png"), "metadata_path": str(metadata_path)}
    finally:
        scene.camera, scene.render.filepath = old_camera, old_filepath
        if temporary_camera is not None:
            data = temporary_camera.data
            bpy.data.objects.remove(temporary_camera, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)


def _asset_candidate(request, live):
    if live:
        raise BlenderError("Asset candidate operations support Headless only")
    source, target = Path(request["source_workspace"]).resolve(), Path(request["workspace"]).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise BlenderError("Asset candidate/source overlap")
    if (target / "scene.blend").exists() or (target / "plan.json").exists():
        raise BlenderError("Asset target already contains a candidate")
    original = _candidate(source, False, frozen=True); plan = _consistent_plan(original, source)
    staging = target / (".asset-fork-" + uuid.uuid4().hex + ".blend")
    before = _data_pointers(); scene = None; committing = False
    try:
        bpy.data.libraries.write(str(staging), {original}, fake_user=True)
        scene = _load_library_scene(staging, original.name)
        scene.name = f"SceneLoop candidate {target.name}"; scene["sceneloop_workspace"] = str(target); _activate(scene)
        if request["action"] == "asset_import":
            plan, row = asset_runtime.import_asset(scene, plan, request["asset_path"], request["asset_source"], request["instance_id"], request.get("transform"))
        else:
            plan, row = asset_runtime.transform_asset(scene, plan, request["instance_id"], request["changes"])
        scene["sceneloop_plan"] = json.dumps(plan, ensure_ascii=False)
        asset_runtime.inspect_assets(scene, plan)
        committing = True
        result = _commit_scene(scene, target, False, plan, preserve_failed=True)
        result["asset"] = row
        return result
    except Exception as error:
        if committing or getattr(error, "outcome", None) == "unknown":
            raise
        _activate(original)
        if scene is not None:
            bpy.data.scenes.remove(scene)
        _remove_temporary_dependencies(before)
        raise BlenderError(f"Asset import/transform rejected before commit: {error}", outcome="rejected") from error
    finally:
        staging.unlink(missing_ok=True)


def _execute_request(request, live=False):
    if live and bpy.context.mode != "OBJECT":
        raise BlenderError("请先让 Blender 退出编辑或雕刻模式，再创建或检查候选")
    workspace = Path(request["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    action = request["action"]
    if action=='dependencies':
        from observation_dependencies import inspect_native
        return inspect_native(workspace)
    if action in {"asset_import", "asset_transform"}:
        return _asset_candidate(request, live)
    if action not in {"build", "build_render", "render", "inspect", "snapshot", "rollback", "finalize", "fork", "edit", "observe", "geometry", "import", "rebind"} or (live and action == "finalize"):
        raise BlenderError(f"不支持的操作 {action}")
    document_workspace = Path(request["source_workspace"]).resolve() if action == "fork" else workspace
    document_path = document_workspace / "plan.json"
    imported = action == "import" or (document_path.is_file() and json.loads(document_path.read_text(encoding="utf-8")).get("scene_kind") == "imported")
    if imported:
        import imported_scene
        return imported_scene.execute_attached(request) if live else imported_scene.execute(request, _object)
    if action == "rebind":
        # Only a materialized isolated copy whose embedded workspace path no longer
        # matches its directory. Re-open the file, verify the plan, re-bind the
        # identity and rebuild dependent products through the ordinary commit path.
        if live:
            raise BlenderError("rebind 仅用于隔离候选副本")
        if not (workspace / "scene.blend").is_file():
            raise BlenderError("候选缺少 scene.blend，无法重绑身份")
        bpy.ops.wm.open_mainfile(filepath=str(workspace / "scene.blend"), load_ui=False, use_scripts=False)
        candidates = [scene for scene in bpy.data.scenes if scene.name.startswith("SceneLoop candidate ")]
        if len(candidates) != 1:
            raise BlenderError("候选文件需要唯一的 SceneLoop 场景")
        scene = candidates[0]
        plan = _consistent_plan(scene, workspace)
        scene["sceneloop_workspace"] = str(workspace)
        return _commit_scene(scene, workspace, False, plan,
                             names=("scene.blend", "inspection.json"))
    if action == "fork":
        source = Path(request["source_workspace"]).resolve()
        if source == workspace or source.is_relative_to(workspace) or workspace.is_relative_to(source):
            raise BlenderError("候选与来源必须是互不包含的独立目录")
        if (workspace / "scene.blend").exists() or (workspace / "plan.json").exists():
            raise BlenderError("新候选已存在，拒绝覆盖")
        original = _candidate(source, live, frozen=request.get("frozen") is True)
        plan = _consistent_plan(original, source)
        staging = workspace / f".fork-{uuid.uuid4().hex}.blend"
        scene = None
        try:
            bpy.data.libraries.write(str(staging), {original}, fake_user=True)
            scene = _load_library_scene(staging, original.name)
            scene.name = f"SceneLoop candidate {workspace.name}"
            scene["sceneloop_workspace"] = str(workspace)
            _activate(scene)
            return _commit_scene(scene, workspace, live, plan)
        except Exception:
            if live:
                _activate(original)
                if scene is not None:
                    bpy.data.scenes.remove(scene)
            raise
        finally:
            staging.unlink(missing_ok=True)
    if action in {"build", "build_render"}:
        if (workspace / "scene.blend").exists():
            raise BlenderError("候选文件已存在，拒绝覆盖")
        plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        source_scene = bpy.context.scene
        scene = create_scene(plan, workspace)
        scene["sceneloop_source_scene"] = source_scene.name
        _save_candidate(scene, workspace, live)
    else:
        scene = _candidate(workspace, live, request.get("scene_workspace") if action == "finalize" else None, frozen=request.get("frozen") is True)
    if action == "geometry":
        import geometry_inspector
        return geometry_inspector.inspect_generated(scene, workspace, request)
    result = {"scene_path": str(workspace / "scene.blend"), "workspace": str(workspace), "mode": "live" if live else "headless"}
    if action == "finalize":
        _save_candidate(scene, workspace, live=False)
    if action == "observe":
        return _observe(scene, workspace, request["view_spec"], request["output_id"])
    if action == "edit":
        # The frozen .blend is the source of truth. Preserve every untouched mesh,
        # modifier, material, camera and light; never reconstruct the scene here.
        plan = _consistent_plan(scene, workspace)
        apply_edit_plan(plan, request["edit_plan"])  # Validate whole request before touching the candidate.
        for operation in request["edit_plan"]["operations"]:
            if operation["op"] in surface_contract.OPS:
                plan = apply_edit_plan(plan, {"operations": [operation]})
                if operation["op"] == "configure_lighting":
                    surface_runtime.apply_lighting(scene, plan["surface"]["lighting"])
                else:
                    surface_runtime.apply_materials(scene, plan)
                scene["sceneloop_plan"] = json.dumps(plan, ensure_ascii=False)
            else:
                plan = apply_incremental_edit(scene, plan, {"operations": [operation]}, _object, _create_part)
                incremental_scene.verify_kind_transforms(scene, getattr(apply_incremental_edit, "kind_expectations", {}) or {})
                # Explicit shape replacement can replace Mesh/material slots. Rebind
                # only our previously verified supported surfaces to the new parts.
                surface_runtime.apply_materials(scene, plan)
        surface_runtime.inspect(scene, plan)
        return _commit_scene(scene, workspace, live, plan)
    if action == "snapshot":
        plan = _consistent_plan(scene, workspace)
        path = workspace / "snapshots" / f"{uuid.uuid4().hex}.blend"
        path.parent.mkdir(exist_ok=True)
        if path.parent.resolve().parent != workspace:
            raise BlenderError("快照目录不能位于候选目录之外")
        bpy.data.libraries.write(str(path), {scene}, fake_user=True)
        path.with_suffix(".json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "plan_path": str(path.with_suffix(".json")), "workspace": str(workspace), "scene_name": scene.name}
    if action == "rollback":
        snapshot = request["snapshot"]
        path = Path(snapshot["path"]).resolve()
        if path.parent != workspace / "snapshots" or path.suffix != ".blend" or not path.with_suffix(".json").is_file():
            raise BlenderError("快照不属于候选目录或缺少对应计划")
        plan = validate_plan(json.loads(path.with_suffix(".json").read_text(encoding="utf-8")))
        restored = _load_library_scene(path, snapshot.get("scene_name"))
        try:
            if restored.get("sceneloop_workspace") != str(workspace) or validate_plan(json.loads(restored.get("sceneloop_plan", "{}"))) != plan:
                raise BlenderError("快照中的候选场景与计划不一致")
            _activate(restored)
            result = _commit_scene(restored, workspace, live, plan)
        except Exception:
            if live:
                _activate(scene)
                bpy.data.scenes.remove(restored)
            raise
        if live:
            bpy.data.scenes.remove(scene)
        return result
    if action in {"build_render", "render"}:
        cameras = [x for x in scene.objects if x.type == "CAMERA"]
        main = next(x for x in cameras if x.name.startswith("SceneLoop main"))
        auxiliary = next(x for x in cameras if x.name.startswith("SceneLoop auxiliary"))
        for view, camera in (("main", main), ("auxiliary", auxiliary)):
            scene.camera = camera
            scene.render.filepath = str(workspace / f"{view}.png")
            with render_execution_slot(scene.render.engine, getattr(scene.cycles, "device", None)):
                bpy.ops.render.render(write_still=True, scene=scene.name)
            result[f"{view}_path"] = scene.render.filepath
            result.setdefault("observations", {})[view] = observation_metadata(scene, camera, json.loads(scene["sceneloop_plan"])["camera"]["target"])
        scene.camera = main
    inspection = inspect_scene(scene)
    inspection_path = workspace / "inspection.json"
    inspection_path.write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
    result["inspection_path"] = str(inspection_path)
    result["inspection"] = inspection
    return result


# Only newly introduced, unused data blocks may be cleaned up. User-owned scene
# objects, including hand-edited displays of older revisions, remain untouched.
_TEMP_DATA = ("collections", "objects", "meshes", "curves", "cameras", "lights", "materials", "worlds", "node_groups", "images")


def _data_pointers():
    return {name: {item.as_pointer() for item in getattr(bpy.data, name)} for name in _TEMP_DATA}


def _remove_temporary_dependencies(before):
    # Parent/material dependencies can become unused after children are removed.
    while True:
        removed = False
        for name in _TEMP_DATA:
            collection = getattr(bpy.data, name)
            for item in list(collection):
                if item.as_pointer() not in before[name] and item.users == int(item.use_fake_user):
                    item.use_fake_user = False
                    collection.remove(item)
                    removed = True
        if not removed:
            return


def execute_request(request, live=False):
    try:
        return _execute_isolated_request(request, live)
    except StructureObservationRefused as error:
        if request.get("action") != "observe":
            raise
        # Both observation paths validate/create this unique directory before
        # entering the inspection scope. A preflight refusal leaves it empty.
        workspace = Path(request["workspace"]).resolve()
        directory = (workspace / "observations" / request["output_id"]).resolve()
        if directory.parent == (workspace / "observations").resolve():
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise BlenderError(
                    f"{error} Temporary empty observation directory cleanup failed: {cleanup_error}",
                    outcome="rejected", code=error.code) from error
        raise BlenderError(str(error), outcome="rejected", code=error.code) from error
    except GeometryValidationError as error:
        # Only the strict geometry refusal is correctable. A commit-window
        # failure has already marked its outcome unknown and must stay unknown.
        outcome = "unknown" if getattr(error, "outcome", None) == "unknown" else "rejected"
        raise BlenderError(str(error), outcome=outcome) from error


def _execute_isolated_request(request, live=False):
    if not live or request.get("frozen") is not True:
        return _execute_request(request, live)
    previous_scene = bpy.context.scene
    scenes_before = {scene.as_pointer() for scene in bpy.data.scenes}
    data_before = _data_pointers()
    succeeded = False
    try:
        result = _execute_request(request, live=True)
        succeeded = True
        return result
    finally:
        # Read-only tools restore the user's display. Successful fork/edit keeps
        # the new candidate visible while leaving any earlier hand edits intact.
        if not succeeded or request.get("action") not in {"import", "fork", "edit"}:
            _activate(previous_scene)
        for scene in list(bpy.data.scenes):
            if scene.as_pointer() not in scenes_before and (not succeeded or scene.get("sceneloop_frozen_input")):
                bpy.data.scenes.remove(scene)
        _remove_temporary_dependencies(data_before)


def main():
    arguments = sys.argv[sys.argv.index("--") + 1:]
    request = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    try:
        result = execute_request(request)
        response = {"ok": True, "result": result}
    except Exception as exc:
        outcome = getattr(exc, "outcome", None)
        if outcome not in ("rejected", "unknown", "not_scheduled"):
            outcome = "rejected" if isinstance(exc, BlenderError) else "unknown"
        response = {"ok": False, "error": str(exc), "traceback": traceback.format_exc(), "outcome": outcome}
        if getattr(exc, "code", None) in {"local_gpu_busy", "local_gpu_lock_unavailable", "local_gpu_lock_platform_unsupported"}:
            response["code"] = exc.code
    Path(request["response_path"]).write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    if not response["ok"]:
        raise BlenderError(response["error"], outcome=response["outcome"])


if __name__ == "__main__":
    main()
