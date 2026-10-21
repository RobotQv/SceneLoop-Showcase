"""Fixed Blender entry point for presentation copies; no bridge or model scripts."""
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def initialize_view():
    scene = bpy.context.scene
    points = [obj.matrix_world @ Vector(corner) for obj in scene.objects
              if obj.type == "MESH" and not obj.hide_viewport for corner in obj.bound_box]
    if points:
        low = Vector([min(point[i] for point in points) for i in range(3)])
        high = Vector([max(point[i] for point in points) for i in range(3)])
        center = (low + high) / 2
        radius = max((high - low).length / 2, 1.)
    else:
        center, radius = Vector((0, 0, 1)), 5.
    count = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.overlay.show_overlays = False
            space.shading.type = "SOLID"
            space.shading.color_type = "MATERIAL"
            space.clip_end = max(1000., radius * 20)
            region = space.region_3d
            if region:
                region.view_perspective = "PERSP"
                region.view_location = center
                region.view_distance = radius * 2.2
                direction = Vector((1, -1.6, 1.1))
                region.view_rotation = (-direction).to_track_quat("-Z", "Y")
                count += 1
    return count


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) != 2 or args[1] not in {"--prepare", "--display"}:
        raise RuntimeError("Unsupported presentation invocation")
    manifest_path = Path(args[0]).resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    display = Path(data["display_path"]).resolve()
    if display != manifest_path.parent / "scene.blend" or data["id"] != manifest_path.parent.name:
        raise RuntimeError("Presentation path/identity mismatch")
    if args[1] == "--prepare":
        source = data["source"]["files"]["blend"]
        if digest(source["path"]) != source["sha256"]:
            raise RuntimeError("Source changed before Blender read")
        bpy.ops.wm.open_mainfile(filepath=source["path"], load_ui=False, use_scripts=False)
        # Same path treatment as ordinary SceneLoop imports; only the copy is saved.
        bpy.ops.file.make_paths_absolute()
        views = initialize_view()
        bpy.ops.wm.save_as_mainfile(filepath=str(display), check_existing=False, relative_remap=False)
        if digest(source["path"]) != source["sha256"]:
            raise RuntimeError("Source changed during presentation preparation")
        (manifest_path.parent / "prepared.json").write_text(json.dumps({"id": data["id"],
            "display_sha256": digest(display), "views_initialized": views,
            "source_scene": bpy.context.scene.name}), encoding="utf-8")
    else:
        if Path(bpy.data.filepath).resolve() != display or digest(display) != data["display_sha256"]:
            raise RuntimeError("Display does not match frozen copy")
        views = initialize_view()
        (manifest_path.parent / "display-ready.json").write_text(json.dumps({"id": data["id"],
            "views_initialized": views, "walk_started": False}), encoding="utf-8")
        print("SceneLoop presentation: F3 > Walk Navigation; WASD, mouse, wheel, Esc. Working is unchanged.", flush=True)


if __name__ == "__main__":
    main()
