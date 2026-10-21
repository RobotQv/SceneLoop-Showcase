"""Blender-side preservation of imported files; never reconstruct user geometry.

All file mutations run in an isolated Blender process. Attached mode delegates
here and only appends a display copy into the user's running Blender afterward.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import uuid

import bpy
from mathutils import Vector

from observation_runtime import aim_camera, observation_metadata, observation_mode

from blender_adapter import (BlenderAdapter, BlenderError, observation_directory,
                            replace_candidate_files, validate_imported_manifest,
                            validate_imported_edit_plan, validate_view_spec)

ID_KEY = "sceneloop_import_id"
RIG_KEY = "sceneloop_comparison_view"
TEMPLATE_KEY = "sceneloop_import_template"


def _activate(scene):
    if bpy.context.window:
        bpy.context.window.scene = scene


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _number(value):
    return round(float(value), 6)


def _vector(value):
    return [_number(component) for component in value]


def _identifier(obj, occupied):
    if obj.library:
        value = "linked_" + hashlib.sha256((obj.library.filepath + "\0" + obj.name).encode()).hexdigest()[:24]
    else:
        value = obj.get(ID_KEY)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) or value in occupied:
            value = "import_" + uuid.uuid4().hex
            obj[ID_KEY] = value
    if value in occupied:
        raise BlenderError("导入场景含无法区分的链接对象")
    occupied.add(value)
    return value


def _bounds(obj, depsgraph):
    if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME", "POINTCLOUD"}:
        return None
    evaluated = obj.evaluated_get(depsgraph)
    points = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
    if not points or not all(math.isfinite(value) for point in points for value in point):
        return None
    return [[_number(min(point[index] for point in points)) for index in range(3)], [_number(max(point[index] for point in points)) for index in range(3)]]


def _group_bounds(obj, depsgraph):
    children = [_bounds(child, depsgraph) for child in obj.children_recursive]
    children = [bounds for bounds in children if bounds]
    if not children:
        return None
    return [[min(bounds[0][index] for bounds in children) for index in range(3)], [max(bounds[1][index] for bounds in children) for index in range(3)]]


def _template_children_readonly(obj, scene, users):
    children = set(obj.children_recursive)
    for child in children:
        if (child.type != "MESH" or child.parent != obj or child.library
                or child.animation_data or child.constraints or child.modifiers or child.children
                or len(child.users_scene) != 1 or scene not in child.users_scene
                or child.get("sceneloop_import_parent") != obj.get(ID_KEY)):
            return True
        if (child.data and (getattr(child.data, "animation_data", None) or getattr(child.data, "shape_keys", None))) or child.rigid_body:
            return True
        if any(not isinstance(user, bpy.types.Collection) and user != obj and user != scene and user not in children for user in users.get(child, set())):
            return True
    return False


def _readonly(obj, scene, users, instanced):
    reasons = []
    managed = bool(obj.get(TEMPLATE_KEY))
    if obj.type != "MESH" and not managed:
        reasons.append("首版只编辑静态 Mesh 或新建参数化物体")
    if obj.library or obj.override_library or (obj.data and getattr(obj.data, "library", None)):
        reasons.append("链接或库覆盖对象只读")
    if obj in instanced:
        reasons.append("属于集合实例，被其他实例共享")
    if managed and _template_children_readonly(obj, scene, users):
        reasons.append("参数化子部件存在额外依赖，只读")
    if obj.parent or (obj.children and not managed):
        reasons.append("存在父子层级，避免间接改变其他物体")
    if len(obj.users_scene) != 1 or scene not in obj.users_scene:
        reasons.append("被多个场景共享")
    if any(abs(value) > .000001 for value in (*obj.delta_location, *obj.delta_rotation_euler)) or any(abs(value - 1) > .000001 for value in obj.delta_scale):
        reasons.append("存在附加变换")
    if obj.constraints:
        reasons.append("存在约束")
    if obj.animation_data or (obj.data and getattr(obj.data, "animation_data", None)):
        reasons.append("存在动画或驱动")
    if obj.data and getattr(obj.data, "shape_keys", None):
        reasons.append("存在形态键")
    if getattr(obj, "rigid_body", None) or getattr(obj, "rigid_body_constraint", None):
        reasons.append("存在物理模拟")
    if any(mod.type in {"NODES", "ARMATURE", "CLOTH", "SOFT_BODY", "FLUID", "PARTICLE_SYSTEM", "MESH_CACHE", "MESH_SEQUENCE_CACHE"} for mod in obj.modifiers):
        reasons.append("存在复杂生成或模拟修改器")
    permitted_children = set(obj.children_recursive) if managed else set()
    if any(not isinstance(user, bpy.types.Collection) and user != scene and user not in permitted_children for user in users.get(obj, set())):
        reasons.append("被其他对象或数据引用")
    if any(collection.library for collection in obj.users_collection):
        reasons.append("处于链接集合")
    if obj.rotation_mode not in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "QUATERNION", "AXIS_ANGLE"}:
        reasons.append("旋转模式不受支持")
    if any(abs(component) > 10000 for component in obj.location) or any(component <= 0 or component > 10000 for component in obj.scale):
        reasons.append("变换超出首版可编辑数值范围")
    return reasons


def _inventory(scene):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    users = bpy.data.user_map()
    instanced = {member for owner in bpy.data.objects if owner.instance_type == "COLLECTION" and owner.instance_collection for member in owner.instance_collection.all_objects}
    occupied = set()
    rows = []
    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.get(RIG_KEY):
            continue
        if obj.parent and obj.parent.get(TEMPLATE_KEY) and obj.get("sceneloop_import_parent") == obj.parent.get(ID_KEY):
            continue
        identifier = _identifier(obj, occupied)
        reasons = _readonly(obj, scene, users, instanced)
        managed = bool(obj.get(TEMPLATE_KEY))
        bounds = _group_bounds(obj, depsgraph) if managed else _bounds(obj, depsgraph)
        rotation = obj.rotation_euler if obj.rotation_mode == "XYZ" else obj.matrix_local.to_euler("XYZ")
        rows.append({"id": identifier, "name": obj.name, "type": obj.type,
                     "kind": obj.get("sceneloop_kind", "imported_mesh" if obj.type == "MESH" else "imported_object"),
                     "position": _vector(obj.matrix_world.translation), "rotation": [_number(math.degrees(value)) for value in rotation],
                     "scale": _vector(obj.scale), "dimensions": [_number(bounds[1][index] - bounds[0][index]) for index in range(3)] if bounds else _vector(obj.dimensions), "bounds": bounds,
                     "editable_operations": [] if reasons else ["update", "remove"],
                     "editable_fields": [] if reasons else ["position", "rotation", "scale"],
                     "readonly_reasons": reasons, "managed_template": managed,
                     "position_semantics": "bottom_center" if managed else "world_object_origin",
                     "mesh_vertices": len(obj.data.vertices) if obj.type == "MESH" else None,
                     "modifiers": [{"name": mod.name, "type": mod.type} for mod in obj.modifiers],
                     "materials": [slot.material.name if slot.material else None for slot in obj.material_slots]})
    return rows


def _center(scene):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bounds = [_bounds(obj, depsgraph) for obj in scene.objects if not obj.hide_render and not obj.get(RIG_KEY)]
    bounds = [value for value in bounds if value]
    if not bounds:
        return Vector((0, 0, 0)), 3.
    lo = Vector([min(value[0][axis] for value in bounds) for axis in range(3)])
    hi = Vector([max(value[1][axis] for value in bounds) for axis in range(3)])
    return (lo + hi) / 2, max((hi - lo).length / 2, .5)


def _camera(scene, collection, name, position, target, lens=45, roll_degrees=0):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    obj.location = position
    aim_camera(obj, target, roll_degrees)
    data.lens = lens
    data.clip_end = 100000
    return obj


def _setup_rig(scene):
    # Existing cameras remain unmodified. Dedicated copies freeze comparisons.
    for existing in scene.objects:
        if existing.get(RIG_KEY):
            del existing[RIG_KEY]
    collection = bpy.data.collections.new("SceneLoop comparison rig")
    scene.collection.children.link(collection)
    collection["sceneloop_import_rig"] = True
    center, radius = _center(scene)
    source = scene.camera
    if source and source.type == "CAMERA" and source.data.type in {"PERSP", "ORTHO"} and max(abs(v) for v in source.matrix_world.translation) <= 10000:
        data = source.data.copy()
        data.animation_data_clear()
        data.lens = max(10, min(200, data.lens))
        main = bpy.data.objects.new("SceneLoop imported main", data)
        collection.objects.link(main)
        main.matrix_world = source.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
        target = main.location + (main.matrix_world.to_quaternion() @ Vector((0, 0, -1))) * max((main.location - center).length, 1)
    else:
        direction = Vector((1.2, -1.6, 1.2)).normalized()
        main = _camera(scene, collection, "SceneLoop imported main", center + direction * radius * 4, center)
        target = center
    direction = main.location - Vector(target)
    auxiliary_position = Vector(target) + Vector((-direction.y, direction.x, max(direction.z, radius * 1.5)))
    auxiliary = _camera(scene, collection, "SceneLoop imported auxiliary", auxiliary_position, target, main.data.lens)
    for view, camera in (("main", main), ("auxiliary", auxiliary)):
        camera[RIG_KEY] = view
        camera["sceneloop_camera_target"] = list(target)
    return {"position": _vector(main.location), "target": _vector(target), "lens": float(main.data.lens)}


def _comparison_camera(scene, view):
    cameras = [obj for obj in scene.objects if obj.type == "CAMERA" and obj.get(RIG_KEY) == view]
    if len(cameras) != 1:
        raise BlenderError("导入场景缺少唯一的固定比较相机")
    return cameras[0]


def _camera_metadata(camera):
    return {"position": _vector(camera.location), "rotation_euler": _vector(camera.rotation_euler),
            "target": list(camera.get("sceneloop_camera_target", (0, 0, 0))),
            "lens": float(camera.data.lens), "type": camera.data.type,
            "ortho_scale": float(camera.data.ortho_scale), "resolution": [960, 640],
            "engine": "CYCLES", "samples": 12, "compositing": False, "sequencer": False}


def _manifest(scene, previous, workspace):
    result = {**previous, "schema_version": 1, "scene_kind": "imported", "scene_name": scene.name,
              "scene_id": scene["sceneloop_import_scene_id"], "objects": _inventory(scene),
              "frame": scene.frame_current, "unit_system": scene.unit_settings.system,
              "unit_scale_length": scene.unit_settings.scale_length,
              "all_scenes": [item.name for item in bpy.data.scenes],
              "comparison": {view: _camera_metadata(_comparison_camera(scene, view)) for view in ("main", "auxiliary")},
              "limitations": ["原文件全部场景和数据保留；只编辑所选场景中可编辑对象", "物体变换沿用原文件单位，不自动转为米", "自动脚本禁用；带动画、约束、链接或复杂依赖的对象只读", "外部贴图/缓存/链接库保留绝对路径引用，尚未打包复制", "观察使用标准分辨率CPU渲染，关闭合成与序列器；原文件对应设置保留", "不代表视觉质量、碰撞或游戏功能已验证"]}
    validate_imported_manifest(result)
    scene["sceneloop_workspace"] = str(workspace)
    scene["sceneloop_scene_kind"] = "imported"
    scene["sceneloop_plan"] = json.dumps(result, ensure_ascii=False)
    return result


def _inspection(scene, manifest):
    rows = manifest["objects"]
    return {"scene": scene.name, "scene_kind": "imported", "object_count": len(rows),
            "actual_object_count": len([obj for obj in scene.objects if not obj.get(RIG_KEY)]),
            "mesh_count": sum(obj.type == "MESH" for obj in scene.objects), "semantic_objects": rows,
            "key_objects": [], "verification": "structural_only", "visual_acceptance": "requires_review",
            "warnings": [], "limitations": manifest["limitations"], "cameras": manifest["comparison"],
            "all_scenes": manifest["all_scenes"], "readonly_count": sum(not row["editable_operations"] for row in rows)}


def _commit(scene, workspace, manifest):
    staged = workspace / (".imported-" + uuid.uuid4().hex)
    staged.mkdir()
    try:
        manifest = _manifest(scene, manifest, workspace)
        inspection = _inspection(scene, manifest)
        (staged / "plan.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (staged / "inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
        # Saving the entire database preserves every other scene and dependency.
        # No bpy.data.scenes.remove() and no template reconstruction occur here.
        _activate(scene)
        bpy.ops.wm.save_as_mainfile(filepath=str(staged / "scene.blend"), check_existing=False, relative_remap=False)
        replace_candidate_files(workspace, staged)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return {"scene_path": str(workspace / "scene.blend"), "plan_path": str(workspace / "plan.json"),
            "inspection_path": str(workspace / "inspection.json"), "inspection": inspection,
            "workspace": str(workspace), "mode": "headless", "scene_kind": "imported", "render_stale": True}


def _load(workspace):
    manifest = validate_imported_manifest(json.loads((workspace / "plan.json").read_text(encoding="utf-8")))
    bpy.ops.wm.open_mainfile(filepath=str(workspace / "scene.blend"), load_ui=False, use_scripts=False)
    scenes = [scene for scene in bpy.data.scenes if scene.get("sceneloop_import_scene_id") == manifest["scene_id"]]
    if len(scenes) != 1 or scenes[0].get("sceneloop_workspace") != str(workspace):
        raise BlenderError("导入候选的场景身份或 workspace 不一致")
    scene = scenes[0]
    _activate(scene)
    return scene, manifest


def _preview(scene, camera, path, target=None, *, mode="appearance"):
    old = {"camera": scene.camera, "engine": scene.render.engine, "resolution_x": scene.render.resolution_x,
           "resolution_y": scene.render.resolution_y, "resolution_percentage": scene.render.resolution_percentage,
           "filepath": scene.render.filepath, "file_format": scene.render.image_settings.file_format,
           "device": scene.cycles.device, "samples": scene.cycles.samples, "use_denoising": scene.cycles.use_denoising,
           "use_compositing": scene.render.use_compositing, "use_sequencer": scene.render.use_sequencer}
    try:
        scene.camera = camera
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 12
        scene.cycles.use_denoising = True
        scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 960, 640, 100
        scene.render.image_settings.file_format = "PNG"
        # File Output compositor nodes can write outside the candidate. Preview
        # the scene directly, retaining those nodes untouched in the saved file.
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        scene.render.filepath = str(path)
        with observation_mode(scene, camera, target, mode) as profile:
            metadata = observation_metadata(scene, camera, target, profile=profile)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            return metadata
    finally:
        scene.camera = old["camera"]
        for field in ("engine", "resolution_x", "resolution_y", "resolution_percentage", "filepath", "use_compositing", "use_sequencer"):
            setattr(scene.render, field, old[field])
        for field in ("device", "samples", "use_denoising"):
            setattr(scene.cycles, field, old[field])
        scene.render.image_settings.file_format = old["file_format"]


def _observe(scene, workspace, view_spec, output_id):
    view = validate_view_spec(view_spec)
    directory = observation_directory(workspace, output_id)
    directory.mkdir(parents=True, exist_ok=False)
    temporary = None
    try:
        if "preset" in view:
            camera = _comparison_camera(scene, view["preset"])
        else:
            temporary = _camera(scene, scene.collection, "SceneLoop temporary imported observation", view["position"], view["target"], view["lens"], view.get("roll_degrees", 0))
            temporary["sceneloop_camera_target"] = view["target"]
            camera = temporary
        actual = _preview(scene, camera, directory / "image.png", camera.get("sceneloop_camera_target"), mode=view.get("mode", "appearance"))
        metadata = {**actual, "workspace": str(workspace), "output_id": output_id,
                    "view_spec": view, "comparison_camera_unchanged": True, "scene_kind": "imported"}
        metadata_path = directory / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**metadata, "path": str(directory / "image.png"), "image_path": str(directory / "image.png"), "metadata_path": str(metadata_path)}
    finally:
        if temporary is not None:
            data = temporary.data
            bpy.data.objects.remove(temporary, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)


def _lookup(scene, identifier):
    matches = [obj for obj in scene.objects if obj.get(ID_KEY) == identifier]
    if len(matches) != 1:
        raise BlenderError("可编辑物体身份不唯一或不存在")
    return matches[0]


def _edit(scene, manifest, edit_plan, make_template):
    # Recompute actual restrictions before touching data, even if the caller
    # supplied stale permission metadata. The original .blend remains unchanged.
    current = {**manifest, "objects": _inventory(scene)}
    edit_plan = validate_imported_edit_plan(current, edit_plan)
    for operation in edit_plan["operations"]:
        if operation["op"] == "add":
            item = operation["object"]
            obj = make_template(item)
            obj[ID_KEY] = item["id"]
            obj[TEMPLATE_KEY] = True
            collection = next((value for value in scene.collection.children if value.get("sceneloop_added_objects")), None)
            if collection is None:
                collection = bpy.data.collections.new("SceneLoop additions")
                collection["sceneloop_added_objects"] = True
                scene.collection.children.link(collection)
            for member in [obj, *obj.children_recursive]:
                for existing in list(member.users_collection):
                    existing.objects.unlink(member)
                collection.objects.link(member)
                if member != obj:
                    member["sceneloop_import_parent"] = item["id"]
            continue
        obj = _lookup(scene, operation["id"])
        if operation["op"] == "remove":
            for child in list(obj.children_recursive) if obj.get(TEMPLATE_KEY) else []:
                bpy.data.objects.remove(child, do_unlink=True)
            bpy.data.objects.remove(obj, do_unlink=True)
        else:
            for field, value in operation["changes"].items():
                if field == "position":
                    obj.location = value
                elif field == "rotation":
                    obj.rotation_mode = "XYZ"
                    obj.rotation_euler = [math.radians(number) for number in value]
                else:
                    obj.scale = value
    bpy.context.view_layer.update()


def execute(request, make_template):
    workspace = Path(request["workspace"]).resolve()
    action = request["action"]
    if action == "import":
        source = Path(request["source_path"]).resolve()
        if not source.is_file() or source.suffix.lower() != ".blend" or source.is_relative_to(workspace):
            raise BlenderError("导入来源无效或与候选目录重叠")
        if (workspace / "scene.blend").exists() or (workspace / "plan.json").exists():
            raise BlenderError("导入候选已存在，拒绝覆盖")
        source_hash = _hash(source)
        original = workspace / "source-original.blend"
        if original.exists():
            raise BlenderError("导入来源快照已存在，拒绝覆盖")
        shutil.copy2(source, original)
        if _hash(original) != source_hash:
            raise BlenderError("源文件在创建快照时发生变化")
        # Open at its ORIGINAL path so relative textures, caches and linked
        # libraries resolve correctly. Loading never saves or runs file scripts.
        bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
        if _hash(source) != source_hash:
            raise BlenderError("源文件在读取时发生变化，请重新选择")
        scene_name = request.get("scene_name")
        scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
        if scene is None or scene.library:
            raise BlenderError("请选择文件内现有的本地场景")
        _activate(scene)
        bpy.ops.file.make_paths_absolute()
        scene["sceneloop_import_scene_id"] = uuid.uuid4().hex
        manifest = {"camera": _setup_rig(scene), "source": {"path": str(source), "sha256": source_hash, "snapshot_path": str(original), "snapshot_sha256": source_hash}}
        return _commit(scene, workspace, manifest)
    source_workspace = Path(request["source_workspace"]).resolve() if action == "fork" else workspace
    scene, manifest = _load(source_workspace)
    if action == "geometry":
        from geometry_inspector import inspect_imported
        return inspect_imported(scene, manifest, workspace, request)
    if action == "fork":
        if source_workspace == workspace or source_workspace.is_relative_to(workspace) or workspace.is_relative_to(source_workspace):
            raise BlenderError("来源与候选目录必须独立")
        if (workspace / "scene.blend").exists() or (workspace / "plan.json").exists():
            raise BlenderError("新候选已存在，拒绝覆盖")
        return _commit(scene, workspace, manifest)
    if action == "edit":
        _edit(scene, manifest, request["edit_plan"], make_template)
        return _commit(scene, workspace, manifest)
    if action == "observe":
        return _observe(scene, workspace, request["view_spec"], request["output_id"])
    if action in {"snapshot", "rollback"}:
        adapter = BlenderAdapter(blender_path=bpy.app.binary_path)
        return adapter.create_snapshot(workspace) if action == "snapshot" else adapter.rollback(workspace, request["snapshot"])
    if action not in {"inspect", "render"}:
        raise BlenderError("导入场景不支持此操作")
    result = {"scene_path": str(workspace / "scene.blend"), "workspace": str(workspace), "mode": "headless", "scene_kind": "imported"}
    if action == "render":
        result["observations"] = {}
        for view in ("main", "auxiliary"):
            path = workspace / f"{view}.png"
            camera = _comparison_camera(scene, view)
            result["observations"][view] = _preview(scene, camera, path, camera.get("sceneloop_camera_target"))
            result[f"{view}_path"] = str(path)
    current = _manifest(scene, manifest, workspace)
    inspection = _inspection(scene, current)
    path = workspace / "inspection.json"
    path.write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "inspection": inspection, "inspection_path": str(path), "plan_path": str(workspace / "plan.json")}


def execute_attached(request):
    """Preserve the complete file in a subprocess; append only a display copy."""
    adapter = BlenderAdapter(blender_path=bpy.app.binary_path)
    extra = {key: value for key, value in request.items() if key not in {"workspace", "action", "response_path", "request_id"}}
    result = adapter._run(request["action"], request["workspace"], **extra)
    if request["action"] in {"import", "fork", "edit", "rollback"}:
        workspace = Path(request["workspace"])
        manifest = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        with bpy.data.libraries.load(str(workspace / "scene.blend"), link=False) as (source, target):
            if manifest["scene_name"] not in source.scenes:
                raise BlenderError("候选文件缺少所选场景")
            target.scenes = [manifest["scene_name"]]
        scene = target.scenes[0]
        scene["sceneloop_attached_display_only"] = True
        _activate(scene)
    return {**result, "mode": "attached_file_copy", "execution_note": "导入文件在隔离后台进程保真编辑，当前Blender只显示候选副本；未修改用户原文件或手改场景"}
