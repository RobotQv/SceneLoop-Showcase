"""Data-only, bounded authored surfaces. No files, scripts or arbitrary shader graphs."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re


class SurfaceError(ValueError):
    outcome = "rejected"


RECIPES = {
    "wood": {"roughness": .65, "scale_m": .35, "bump_m": .0007},
    "stone": {"roughness": .78, "scale_m": .12, "bump_m": .0008},
    "metal": {"roughness": .3, "scale_m": .05, "bump_m": .00004},
    "cloth": {"roughness": .65, "scale_m": .002, "bump_m": .00015},
    "ground": {"roughness": .88, "scale_m": .12, "bump_m": .003},
    "paint": {"roughness": .45, "scale_m": .1, "bump_m": .00005},
}
OPS = frozenset({"assign_surface", "configure_lighting"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def number(value, low, high, name):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise SurfaceError(f"{name} must be a finite number in {low}..{high}")
    return float(value)


def vector(value, low, high, name):
    if not isinstance(value, list) or len(value) != 3:
        raise SurfaceError(f"{name} requires three numbers")
    return [number(v, low, high, name) for v in value]


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise SurfaceError("Surface target/light ID is invalid")
    return value


def target(value, plan):
    if not isinstance(value, dict):
        raise SurfaceError("Surface target must be an object")
    result = copy.deepcopy(value)
    if "object_id" in value:
        if set(value) - {"object_id", "part_id", "slot"}:
            raise SurfaceError("Surface target only accepts object_id/part_id/slot")
        obj = next((o for o in plan["objects"] if o["id"] == identifier(value["object_id"])), None)
        if obj is None:
            raise SurfaceError("Surface target object does not exist")
        if "part_id" in value:
            identifier(value["part_id"])
            if obj["kind"] == "compound" and value["part_id"] not in {p.get("id") for p in obj["parts"]}:
                raise SurfaceError("Surface target part does not exist")
    elif "instance_id" in value:
        if set(value) - {"instance_id", "node_id", "slot"}:
            raise SurfaceError("Surface target only accepts instance_id/node_id/slot")
        asset = next((a for a in plan.get("assets", {}).get("instances", []) if a["instance_id"] == identifier(value["instance_id"])), None)
        if asset is None:
            raise SurfaceError("Surface target asset does not exist")
        if "node_id" in value:
            identifier(value["node_id"])
            if value["node_id"] not in {n["node_id"] for n in asset["nodes"] if n["type"] == "MESH"}:
                raise SurfaceError("Surface target mesh node does not exist")
    else:
        raise SurfaceError("Surface requires object_id or instance_id")
    slot = value.get("slot", 0)
    if type(slot) is not int or not 0 <= slot <= 15:
        raise SurfaceError("Surface slot must be 0..15")
    result["slot"] = slot
    return result


def surface(value):
    if not isinstance(value, dict) or set(value) - {"recipe", "color", "roughness", "scale_m", "bump_m", "direction", "variation"}:
        raise SurfaceError("Unsupported surface field")
    recipe = value.get("recipe")
    if not isinstance(recipe, str) or recipe not in RECIPES:
        raise SurfaceError("Unknown surface recipe; choose wood/stone/metal/cloth/ground/paint")
    result = {"recipe": recipe, "color": vector(value.get("color"), 0, 1, "sRGB color")}
    for name, low, high in (("roughness", 0, 1), ("scale_m", .001, 20), ("bump_m", 0, .02)):
        result[name] = number(value.get(name, RECIPES[recipe][name]), low, high, name)
    result["direction"] = value.get("direction", "Z")
    if result["direction"] not in ("X", "Y", "Z"):
        raise SurfaceError("Surface direction must be world X/Y/Z")
    result["variation"] = number(value.get("variation", .25), 0, 1, "variation")
    return result


def lighting(value):
    if not isinstance(value, dict) or set(value) != {"world", "lights"}:
        raise SurfaceError("Lighting requires world and lights")
    world = value["world"]
    if not isinstance(world, dict):
        raise SurfaceError("Lighting world is invalid")
    kind = world.get("type")
    if kind == "sky" and set(world) == {"type", "strength", "sun_elevation", "sun_rotation"}:
        w = {"type": kind, "strength": number(world["strength"], 0, 5, "sky strength"),
             "sun_elevation": number(world["sun_elevation"], -5, 90, "sun_elevation"),
             "sun_rotation": number(world["sun_rotation"], -360, 360, "sun_rotation")}
    elif kind == "color" and set(world) == {"type", "strength", "color"}:
        w = {"type": kind, "strength": number(world["strength"], 0, 5, "world strength"),
             "color": vector(world["color"], 0, 1, "world color")}
    else:
        raise SurfaceError("World supports only bounded sky or color parameters")
    lights = value["lights"]
    if not isinstance(lights, list) or len(lights) > 8:
        raise SurfaceError("At most eight managed lights are allowed")
    rows, ids = [], set()
    for row in lights:
        if not isinstance(row, dict):
            raise SurfaceError("Light must be an object")
        kind = row.get("type")
        fields = {"id", "type", "energy", "color"} | ({"rotation", "angle"} if kind == "SUN" else {"position", "size", "target"} if kind == "AREA" else {"position", "size"})
        if kind not in ("SUN", "AREA", "POINT") or set(row) != fields:
            raise SurfaceError("Unsupported light type or fields")
        name = identifier(row["id"])
        if name in ids:
            raise SurfaceError("Duplicate light ID")
        ids.add(name)
        item = {"id": name, "type": kind, "color": vector(row["color"], 0, 1, "light color"),
                "energy": number(row["energy"], 0, 10 if kind == "SUN" else 10000, "light energy")}
        if kind == "SUN":
            item.update(rotation=vector(row["rotation"], -360, 360, "light rotation"), angle=number(row["angle"], 0, 30, "sun angle"))
        else:
            item.update(position=vector(row["position"], -1000, 1000, "light position"), size=number(row["size"], .01, 100, "light size"))
            if kind == "AREA":
                item["target"] = vector(row["target"], -1000, 1000, "light target")
                if sum((a-b)**2 for a,b in zip(item["target"], item["position"])) < .01:
                    raise SurfaceError("Area light target is too close to its position")
        rows.append(item)
    return {"world": w, "lights": sorted(rows, key=lambda r:r["id"])}


def normalize(value, plan):
    if not isinstance(value, dict) or set(value) - {"schema_version", "assignments", "lighting"} or value.get("schema_version") != 1:
        raise SurfaceError("Surface state needs schema_version=1")
    rows = value.get("assignments")
    if not isinstance(rows, list) or len(rows) > 128:
        raise SurfaceError("Surface assignments must be a list of at most 128 entries")
    result = {"schema_version": 1, "assignments": []}; seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"target", "surface"}:
            raise SurfaceError("Surface assignment requires target and surface")
        t = target(row["target"], plan); key = canonical(t)
        if key in seen:
            raise SurfaceError("Duplicate surface target")
        seen.add(key); result["assignments"].append({"target": t, "surface": surface(row["surface"])})
    result["assignments"].sort(key=lambda r:canonical(r["target"]))
    if "lighting" in value:
        result["lighting"] = lighting(value["lighting"])
    return result


def apply(plan, operation):
    result = copy.deepcopy(plan)
    state = result.setdefault("surface", {"schema_version": 1, "assignments": []})
    if operation.get("op") == "assign_surface" and set(operation) == {"op", "target", "surface"}:
        t = target(operation["target"], result)
        state["assignments"] = [r for r in state["assignments"] if r["target"] != t]
        state["assignments"].append({"target": t, "surface": surface(operation["surface"])})
    elif operation.get("op") == "configure_lighting" and set(operation) == {"op", "lighting"}:
        state["lighting"] = lighting(operation["lighting"])
    else:
        raise SurfaceError("Invalid surface operation")
    result["surface"] = normalize(state, result)
    return result


def prune_removed(plan, operation):
    """Only explicit geometry removals retire the matching authored bindings."""
    if "surface" not in plan:
        return
    if operation["op"] == "remove":
        plan["surface"]["assignments"] = [r for r in plan["surface"]["assignments"] if r["target"].get("object_id") != operation["id"]]
    elif operation["op"] == "remove_part":
        plan["surface"]["assignments"] = [r for r in plan["surface"]["assignments"] if not (r["target"].get("object_id") == operation["id"] and r["target"].get("part_id") == operation["part_id"])]
