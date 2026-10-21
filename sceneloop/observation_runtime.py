"""Blender-only camera aiming and actual observation metadata shared by scene kinds."""
from __future__ import annotations

import math
from contextlib import contextmanager

import bpy
from mathutils import Quaternion, Vector

from observation_contract import observation_identity, APPEARANCE_PROFILE, STRUCTURE_PROFILE_VERSION


def _number(value):
    result = round(float(value), 7)
    return 0.0 if result == 0 else result


def _vector(value):
    return [_number(item) for item in value]


def aim_camera(camera, target, roll_degrees=0):
    """Aim local -Z at target, then rotate around local +Z (right-hand, degrees).

    Cameras constructed by observation tools have no parents or constraints.
    Aiming never changes any other camera or a scene's active camera.
    """
    direction = Vector(target) - camera.location
    if direction.length_squared < .01:
        raise ValueError("Camera position must differ from its target")
    orientation = direction.to_track_quat("-Z", "Y")
    if roll_degrees:
        orientation = orientation @ Quaternion((0, 0, 1), math.radians(roll_degrees))
    camera.rotation_mode = "XYZ"
    camera.rotation_euler = orientation.to_euler("XYZ")
    return camera


def _rna_settings(owner, exclude=()):
    """Read effective scalar/array RNA settings, never object links or scene contents."""
    if owner is None:
        return None
    result = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if name in exclude or name == "rna_type" or prop.type in {"POINTER", "COLLECTION"} or prop.is_readonly:
            continue
        try:
            value = getattr(owner, name)
            if getattr(prop, "is_array", False):
                result[name] = [_number(item) if isinstance(item, float) else item for item in value]
            elif isinstance(value, float):
                result[name] = _number(value)
            elif isinstance(value, (str, bool, int)):
                result[name] = value
            elif isinstance(value, set):
                result[name] = sorted(value)
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _color_curves(mapping):
    if mapping is None:
        return None
    return {"settings": _rna_settings(mapping),
            "curves": [[{"location": _vector(point.location), "handle_type": point.handle_type}
                        for point in curve.points] for curve in mapping.curves]}


def observation_metadata(scene, camera, target=None, *, profile=None):
    """Call while preview overrides are ACTIVE, before restoring the user's settings.

    Identities describe cameras/settings, not revision geometry or output paths.
    Evidence caching must additionally bind the frozen revision and asset digests.
    """
    bpy.context.view_layer.update()
    matrix = camera.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world
    data = camera.data
    camera_state = {"matrix_world": [_vector(row) for row in matrix],
                    "type": data.type, "lens": _number(data.lens),
                    "ortho_scale": _number(data.ortho_scale),
                    "sensor_fit": data.sensor_fit, "sensor_width": _number(data.sensor_width),
                    "sensor_height": _number(data.sensor_height),
                    "shift_x": _number(data.shift_x), "shift_y": _number(data.shift_y),
                    "clip_start": _number(data.clip_start), "clip_end": _number(data.clip_end),
                    "dof": _rna_settings(data.dof), "panorama": _rna_settings(data, exclude={"name", "name_full"})}
    if data.dof.focus_object is not None:
        camera_state["dof_focus_position"] = _vector(data.dof.focus_object.matrix_world.translation)
    settings = {"schema_version": 1, "render": _rna_settings(scene.render, exclude={"filepath"}),
                "cycles": _rna_settings(scene.cycles),
                "image_settings": _rna_settings(scene.render.image_settings),
                "image_view_settings": _rna_settings(getattr(scene.render.image_settings, "view_settings", None)),
                "image_display_settings": _rna_settings(getattr(scene.render.image_settings, "display_settings", None)),
                "image_color_space": _rna_settings(getattr(scene.render.image_settings, "linear_colorspace_settings", None)),
                "image_color_curves": _color_curves(getattr(getattr(scene.render.image_settings, "view_settings", None), "curve_mapping", None)),
                "view_settings": _rna_settings(scene.view_settings),
                "display_settings": _rna_settings(scene.display_settings),
                "color_curves": _color_curves(scene.view_settings.curve_mapping),
                "frame": scene.frame_current, "subframe": _number(scene.frame_subframe)}
    view = {**camera_state, "position": _vector(matrix.translation),
            "rotation_euler": _vector(matrix.to_euler("XYZ")),
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "resolution_percentage": scene.render.resolution_percentage,
            "engine": scene.render.engine, "samples": scene.cycles.samples,
            "view_transform": scene.view_settings.view_transform,
            "compositing": scene.render.use_compositing, "sequencer": scene.render.use_sequencer}
    if target is not None:
        view["target"] = _vector(target)
    profile = profile or APPEARANCE_PROFILE
    return {"observation_mode": profile["mode"], "observation_profile": profile,
            "observation_profile_id": observation_identity(profile),
            "camera": view, "camera_id": observation_identity(camera_state),
            "render_settings": settings, "render_settings_id": observation_identity(settings)}


class StructureObservationRefused(ValueError):
    code = "structure_observation_unsupported"
    outcome = "rejected"

    def __init__(self, reason):
        super().__init__(f"{self.code}: {reason} Select appearance explicitly; no structure image was produced.")


def _render_source_lights(scene):
    """Follow authored collection instances, including render-only dependencies.

    A viewport depsgraph omits sources behind hide_viewport/excluded layers.
    Walk only the current Scene's collection references instead; do not hide
    instancers or their geometry, or inspect unrelated Scenes' light data.
    """
    pending, visited, lights = [scene.collection], set(), {}
    while pending:
        collection = pending.pop()
        identity = collection.as_pointer()
        if identity in visited:
            continue
        visited.add(identity)
        pending.extend(collection.children)
        for obj in collection.objects:
            if (obj.instance_type not in {"NONE", "COLLECTION"}
                    or getattr(obj, "particle_systems", ())
                    or any(modifier.type in {"NODES", "PARTICLE_SYSTEM", "PARTICLE_INSTANCE"}
                           for modifier in getattr(obj, "modifiers", ()))):
                raise StructureObservationRefused(
                    f"Cannot verify generated light sources for {obj.name}; Geometry Nodes, particles and generated instancers are not supported in structure mode.")
            if obj.type == "LIGHT":
                lights[obj.as_pointer()] = obj
            if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
                pending.append(obj.instance_collection)
    return list(lights.values())


@contextmanager
def observation_mode(scene, camera, target, mode="appearance"):
    """Temporary inspection surface/lighting; never save or alter authored data."""
    if mode == "appearance":
        yield APPEARANCE_PROFILE
        return
    if mode != "structure":
        raise ValueError("Invalid observation mode")
    old_world = scene.world
    layers = [(layer, layer.material_override) for layer in scene.view_layers]
    lights = [(obj, obj.hide_render) for obj in _render_source_lights(scene)]
    for obj, hidden in lights:
        if not hidden and obj.is_property_readonly("hide_render"):
            raise StructureObservationRefused(f"Cannot safely exclude authored inspection light: {obj.name}")
    render_fields = ("use_compositing", "use_sequencer", "film_transparent", "use_single_layer")
    old_render = {key: getattr(scene.render, key) for key in render_fields}
    color_fields = ("view_transform", "look", "exposure", "gamma", "use_curve_mapping")
    old_color = {key: getattr(scene.view_settings, key) for key in color_fields}
    material = world = None
    created = []
    try:
        material = bpy.data.materials.new("SceneLoop temporary inspection neutral")
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = (.55, .55, .55, 1)
        shader.inputs["Roughness"].default_value = .8
        shader.inputs["Metallic"].default_value = 0
        shader.inputs["Emission Color"].default_value = (0, 0, 0, 1)
        shader.inputs["Emission Strength"].default_value = 0
        for layer, _ in layers:
            layer.material_override = material
        for obj, _ in lights:
            if not obj.hide_render:
                obj.hide_render = True
        world = bpy.data.worlds.new("SceneLoop temporary inspection world")
        world.use_nodes = True
        background = world.node_tree.nodes.get("Background")
        background.inputs["Color"].default_value = (1, 1, 1, 1)
        background.inputs["Strength"].default_value = .05
        scene.world = world
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        scene.render.film_transparent = False
        scene.render.use_single_layer = True
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0
        scene.view_settings.gamma = 1
        scene.view_settings.use_curve_mapping = False
        bpy.context.view_layer.update()
        matrix = camera.matrix_world
        position = matrix.translation.copy()
        orientation = matrix.to_quaternion()
        aim = Vector(target) if target is not None else position + orientation @ Vector((0, 0, -10))
        distance = max((position - aim).length, 1)
        right, up = orientation @ Vector((1, 0, 0)), orientation @ Vector((0, 1, 0))
        toward_camera = position - aim
        locations = [aim + toward_camera * .7 + up * distance * .8 + right * distance * .6,
                     aim + toward_camera * .8 - right * distance * .8 + up * distance * .3,
                     aim - toward_camera * .7 + up * distance * .8]
        actual_lights = []
        for location, power in zip(locations, (10, 6, 8)):
            data = bpy.data.lights.new("SceneLoop temporary inspection light", type="AREA")
            obj = bpy.data.objects.new("SceneLoop temporary inspection light", data)
            created.append((obj, data))
            scene.collection.objects.link(obj)
            obj.location = location
            obj.rotation_euler = (aim - location).to_track_quat("-Z", "Y").to_euler()
            data.energy, data.size = power * distance ** 2, distance * .5
            data.color = (1, 1, 1)
            actual_lights.append({"type": data.type, "location": _vector(obj.location),
                                  "rotation": _vector(obj.rotation_euler), "energy": _number(data.energy),
                                  "size": _number(data.size), "color": _vector(data.color)})
        profile = {"schema_version": 1, "mode": mode, "version": STRUCTURE_PROFILE_VERSION,
                   "material": {"shader": "Principled BSDF", "base_color": _vector(shader.inputs["Base Color"].default_value),
                                "roughness": _number(shader.inputs["Roughness"].default_value), "metallic": 0, "emission": 0},
                   "world": {"color": _vector(background.inputs["Color"].default_value),
                             "strength": _number(background.inputs["Strength"].default_value)},
                   "lights": actual_lights,
                   "color_management": {key: getattr(scene.view_settings, key) for key in color_fields},
                   "original_lights": "excluded", "compositing": False, "sequencer": False}
        yield profile
    finally:
        for layer, override in layers:
            layer.material_override = override
        for obj, hidden in lights:
            if obj.hide_render != hidden:
                obj.hide_render = hidden
        scene.world = old_world
        for key, value in old_render.items():
            setattr(scene.render, key, value)
        for key, value in old_color.items():
            setattr(scene.view_settings, key, value)
        for obj, data in created:
            bpy.data.objects.remove(obj, do_unlink=True)
            if data.users == 0:
                bpy.data.lights.remove(data)
        if world is not None and world.users == 0:
            bpy.data.worlds.remove(world)
        if material is not None and material.users == 0:
            bpy.data.materials.remove(material)
