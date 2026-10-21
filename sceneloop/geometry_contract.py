"""Bounded, data-only geometry shared by validation and Blender builders."""
from __future__ import annotations

import copy
import math
import re

PART_SHAPES = {"box", "cylinder", "sphere", "cone", "mesh", "extrusion", "tube"}
GEOMETRY_SHAPES = {"mesh", "extrusion", "tube"}
MAX_SCENE_VERTICES = 50000
MAX_SCENE_FACES = 100000


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 10000 or (positive and value <= 0):
        raise ValueError(f"{label}: expected a finite bounded number")
    return float(value)


def _vector(value, size, label):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{label}: expected {size} coordinates")
    return [_number(x, label) for x in value]


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _unit(a):
    length = math.sqrt(_dot(a, a))
    if length < 1e-8:
        raise ValueError("Degenerate direction or reversing tube segment")
    return [x/length for x in a]


def _bounds(points, radius=0):
    if any(max(p[i] for p in points)-min(p[i] for p in points)+2*radius > 100 for i in range(3)):
        raise ValueError("Part geometry cannot exceed 100 local units per axis")


def _orient(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _segments_touch(a, b, c, d):
    values = (_orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b))
    if values[0]*values[1] < -1e-16 and values[2]*values[3] < -1e-16:
        return True
    for orientation, p, q, r in ((values[0], a, b, c), (values[1], a, b, d), (values[2], c, d, a), (values[3], c, d, b)):
        if abs(orientation) <= 1e-8 and all(min(p[i], q[i])-1e-8 <= r[i] <= max(p[i], q[i])+1e-8 for i in (0, 1)):
            return True
    return False


def normalize_part(value, index=0):
    if not isinstance(value, dict) or not isinstance(value.get("shape"), str) or value.get("shape") not in PART_SHAPES:
        raise ValueError("Unsupported part shape")
    result = copy.deepcopy(value)
    identifier = result.setdefault("id", f"part_{index+1:03d}")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
        raise ValueError("Part id must be a stable ASCII identifier")
    if "role" in result and (not isinstance(result["role"], str) or len(result["role"]) > 160):
        raise ValueError("Part role must be a short description")
    result["position"] = _vector(result.get("position", [0, 0, 0]), 3, "part.position")
    result["rotation"] = _vector(result.get("rotation", [0, 0, 0]), 3, "part.rotation")
    shape = result["shape"]
    fields = {"id", "role", "shape", "position", "rotation"}
    if shape not in GEOMETRY_SHAPES:
        fields.add("dimensions")
        result["dimensions"] = _vector(result.get("dimensions"), 3, "part.dimensions")
        if any(x <= 0 for x in result["dimensions"]):
            raise ValueError("Part dimensions must be positive and bounded")
    elif shape == "mesh":
        fields |= {"vertices", "faces"}
        vertices, faces = result.get("vertices"), result.get("faces")
        if not isinstance(vertices, list) or not 3 <= len(vertices) <= 2048:
            raise ValueError("Mesh needs 3..2048 vertices")
        vertices = [_vector(v, 3, "mesh.vertex") for v in vertices]
        if not isinstance(faces, list) or not 1 <= len(faces) <= 4096:
            raise ValueError("Mesh needs 1..4096 faces")
        seen_faces = {}
        for face_index, face in enumerate(faces):
            if not isinstance(face, list) or not 3 <= len(face) <= 16 or any(type(i) is not int or not 0 <= i < len(vertices) for i in face) or len(set(face)) != len(face):
                raise ValueError("Mesh face indices must be distinct valid integers (3..16 per face)")
            # Distinct indices make the smallest vertex a unique ring anchor.
            # Preserve winding/order in the actual mesh; canonicalize only the key.
            anchor = face.index(min(face))
            ring = tuple(face[anchor:] + face[:anchor])
            key = min(ring, (ring[0],) + tuple(reversed(ring[1:])))
            if key in seen_faces:
                raise ValueError(
                    f"part_id={identifier}: mesh faces[{face_index}] duplicates "
                    f"faces[{seen_faces[key]}] (0-based; same vertex ring, including reversed winding)")
            seen_faces[key] = face_index
            normal = [0., 0., 0.]
            for n, index_value in enumerate(face):
                a, b = vertices[index_value], vertices[face[(n+1) % len(face)]]
                cross = _cross(a, b)
                normal = [x+y for x, y in zip(normal, cross)]
            if _dot(normal, normal) < 1e-16:
                raise ValueError("Mesh face is degenerate")
        _bounds(vertices)
        result["vertices"] = vertices
    elif shape == "extrusion":
        fields |= {"profile", "depth"}
        profile = result.get("profile")
        if not isinstance(profile, list) or not 3 <= len(profile) <= 64:
            raise ValueError("Extrusion profile needs 3..64 distinct XY points")
        profile = [_vector(p, 2, "extrusion.profile") for p in profile]
        if len({tuple(p) for p in profile}) != len(profile):
            raise ValueError("Extrusion profile repeats a point; do not repeat its closing point")
        for i, a in enumerate(profile):
            b = profile[(i+1) % len(profile)]
            for j in range(i+1, len(profile)):
                if j == i+1 or (i == 0 and j == len(profile)-1):
                    continue
                if _segments_touch(a, b, profile[j], profile[(j+1) % len(profile)]):
                    raise ValueError("Extrusion profile must not self-intersect")
        area = sum(p[0]*profile[(i+1) % len(profile)][1]-profile[(i+1) % len(profile)][0]*p[1] for i, p in enumerate(profile))
        if abs(area) < 1e-8:
            raise ValueError("Extrusion profile has zero area")
        result["profile"] = profile if area > 0 else list(reversed(profile))
        result["depth"] = _number(result.get("depth"), "extrusion.depth", positive=True)
        _bounds([[p[0], p[1], z] for p in profile for z in (0, result["depth"])])
    else:
        fields |= {"points", "radius", "sides"}
        points = result.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= 128:
            raise ValueError("Tube needs 2..128 path points")
        result["points"] = [_vector(p, 3, "tube.point") for p in points]
        result["radius"] = _number(result.get("radius"), "tube.radius", positive=True)
        result["sides"] = result.get("sides", 12)
        if type(result["sides"]) is not int or not 4 <= result["sides"] <= 24:
            raise ValueError("Tube sides must be an integer in 4..24")
        _bounds(result["points"], result["radius"])
        directions = [_unit(_sub(b, a)) for a, b in zip(result["points"], result["points"][1:])]
        for a, b in zip(directions, directions[1:]):
            _unit([x+y for x, y in zip(a, b)])
    if set(result) - fields:
        raise ValueError("Unexpected geometry fields: " + ", ".join(sorted(set(result)-fields)))
    return result


def normalize_parts(values):
    if not isinstance(values, list) or not 2 <= len(values) <= 50:
        raise ValueError("compound needs 2..50 parts")
    result = [normalize_part(p, i) for i, p in enumerate(values)]
    if len({p["id"] for p in result}) != len(result):
        raise ValueError("Compound part ids must be unique")
    return result


def mesh_data(part):
    """Return authored local vertices/faces; no fitting, script execution or IO."""
    shape = part["shape"]
    if shape == "mesh":
        return copy.deepcopy(part["vertices"]), copy.deepcopy(part["faces"])
    if shape == "extrusion":
        profile, depth = part["profile"], part["depth"]
        count = len(profile)
        vertices = [[p[0], p[1], z] for z in (0, depth) for p in profile]
        faces = [list(reversed(range(count))), list(range(count, 2*count))]
        faces += [[i, (i+1) % count, (i+1) % count+count, i+count] for i in range(count)]
        return vertices, faces
    if shape != "tube":
        raise ValueError("mesh_data only handles mesh/extrusion/tube")
    points, radius, sides = part["points"], part["radius"], part["sides"]
    directions = [_unit(_sub(b, a)) for a, b in zip(points, points[1:])]
    tangents = [directions[0]] + [_unit([x+y for x, y in zip(a, b)]) for a, b in zip(directions, directions[1:])] + [directions[-1]]
    vertices, previous_normal = [], None
    for point, tangent in zip(points, tangents):
        normal = None
        if previous_normal is not None:
            projection = _dot(previous_normal, tangent)
            candidate = [a-projection*b for a, b in zip(previous_normal, tangent)]
            if _dot(candidate, candidate) > 1e-12:
                normal = _unit(candidate)
        if normal is None:
            axis = min(([1, 0, 0], [0, 1, 0], [0, 0, 1]), key=lambda a: abs(_dot(a, tangent)))
            normal = _unit(_cross(tangent, axis))
        binormal = _cross(tangent, normal)
        for side in range(sides):
            angle = 2*math.pi*side/sides
            vertices.append([point[i]+radius*(math.cos(angle)*normal[i]+math.sin(angle)*binormal[i]) for i in range(3)])
        previous_normal = normal
    faces = [list(reversed(range(sides))), list(range((len(points)-1)*sides, len(points)*sides))]
    for ring in range(len(points)-1):
        for side in range(sides):
            next_side = (side+1) % sides
            faces.append([ring*sides+side, ring*sides+next_side, (ring+1)*sides+next_side, (ring+1)*sides+side])
    return vertices, faces


def geometry_cost(part):
    if part["shape"] in GEOMETRY_SHAPES:
        vertices, faces = mesh_data(part)
        return len(vertices), len(faces)
    return {"box": (8, 6), "cylinder": (34, 48), "cone": (18, 32), "sphere": (114, 128)}[part["shape"]]
