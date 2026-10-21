"""Trusted generic mesh builder. No model-provided Python is executed."""
from __future__ import annotations

import math
import bpy

try:
    from .geometry_contract import mesh_data
except ImportError:
    from geometry_contract import mesh_data


class GeometryValidationError(ValueError):
    """Strict mesh validation changed authored geometry before any scene commit."""


def create_part(parent, spec, material=None, label=None):
    vertices, faces = mesh_data(spec)
    mesh = bpy.data.meshes.new(f"{parent.name}/{spec['id']}")
    try:
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        if mesh.validate(verbose=False, clean_customdata=False):
            raise GeometryValidationError(
                f"object_id={parent.get('sceneloop_id', parent.name)} part_id={spec['id']}: "
                "Blender mesh.validate rejected generated geometry because validation changed "
                "the mesh; correct vertices/faces before retrying; candidate was not committed")
        obj = bpy.data.objects.new(f"{parent.name}/{label or spec['id']}", mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj.parent = parent
        obj.location = spec["position"]
        obj.rotation_euler = tuple(math.radians(v) for v in spec["rotation"])
        obj["sceneloop_part_id"] = spec["id"]
        obj["sceneloop_part_role"] = spec.get("role", "")
        if material is not None:
            mesh.materials.append(material)
        return obj
    except Exception:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise
