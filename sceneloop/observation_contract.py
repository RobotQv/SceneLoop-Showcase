"""Camera input and immutable observation identities; no Blender dependency."""
from __future__ import annotations

import hashlib
import json
import math
import re


def _vector(value, field):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(item) or abs(item) > 10000 for item in value)):
        raise ValueError(f"{field} requires three finite numbers within -10000..10000")
    return [float(item) for item in value]


def normalize_view_spec(value):
    """Keep legacy output unchanged; roll is an optional local +Z rotation in degrees."""
    if not isinstance(value, dict):
        raise ValueError("Observation view must be an object")
    mode = value.get("mode", "appearance")
    if mode not in ("appearance", "structure"):
        raise ValueError("Observation mode must be appearance or structure")
    value = dict(value)
    value.pop("mode", None)
    if "preset" in value:
        if set(value) != {"preset"} or value["preset"] not in ("main", "auxiliary"):
            raise ValueError("Only the main and auxiliary presets are supported")
        return {**value, **({"mode": mode} if mode == "structure" else {})}
    if (set(value) - {"position", "target", "lens", "roll_degrees"}
            or not {"position", "target"} <= set(value)):
        raise ValueError("Custom views require position and target; lens and roll_degrees are optional")
    result = {"position": _vector(value["position"], "view.position"),
              "target": _vector(value["target"], "view.target"), "lens": value.get("lens", 50)}
    if sum((a - b) ** 2 for a, b in zip(result["position"], result["target"])) < .01:
        raise ValueError("Camera position must differ from its target")
    lens = result["lens"]
    if isinstance(lens, bool) or not isinstance(lens, (int, float)) or not math.isfinite(lens) or not 10 <= lens <= 200:
        raise ValueError("view.lens must be within 10..200 mm")
    if "roll_degrees" in value:
        roll = value["roll_degrees"]
        if isinstance(roll, bool) or not isinstance(roll, (int, float)) or not math.isfinite(roll) or not -360 <= roll <= 360:
            raise ValueError("view.roll_degrees must be finite and within -360..360 degrees")
        result["roll_degrees"] = float(roll)
    if mode == "structure":
        result["mode"] = mode
    return result


def observation_identity(value):
    """Hash canonical measured settings; callers keep revision/content identities separate."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


APPEARANCE_PROFILE = {"schema_version": 1, "mode": "appearance"}
STRUCTURE_PROFILE_VERSION = "neutral_inspection_v1"
OBSERVATION_FIELDS = ("camera", "camera_id", "render_settings_id", "observation_mode",
                      "observation_profile", "observation_profile_id")


def validate_observation_metadata(value, requested_view=None):
    """Legacy is appearance only; structure needs an actual, internally consistent receipt."""
    if not isinstance(value, dict):
        raise ValueError("Observation receipt must be an object")
    mode = value.get("observation_mode", "appearance")
    if mode not in ("appearance", "structure"):
        raise ValueError("Invalid actual observation mode")
    if requested_view is not None and mode != requested_view.get("mode", "appearance"):
        raise ValueError("Observation receipt mode differs from the request")
    profile = value.get("observation_profile")
    identity = value.get("observation_profile_id")
    if mode == "structure":
        if (not isinstance(profile, dict) or profile.get("mode") != mode
                or profile.get("schema_version") != 1 or profile.get("version") != STRUCTURE_PROFILE_VERSION
                or not {"material", "world", "lights", "color_management"} <= set(profile)
                or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                       for key in ("camera_id", "render_settings_id"))):
            raise ValueError("Structure observation lacks its actual inspection profile or camera/settings")
    elif profile is None and identity is None:
        profile, identity = APPEARANCE_PROFILE, observation_identity(APPEARANCE_PROFILE)
    elif profile != APPEARANCE_PROFILE:
        raise ValueError("Invalid appearance observation profile")
    if identity != observation_identity(profile):
        raise ValueError("Observation profile identity mismatch")
    return {"observation_mode": mode, "observation_profile": profile, "observation_profile_id": identity,
            **{key: value[key] for key in ("camera", "camera_id", "render_settings_id") if key in value}}


def comparison_identity(value):
    actual = validate_observation_metadata(value)
    camera, settings = actual.get("camera_id"), actual.get("render_settings_id")
    if bool(camera) != bool(settings):
        raise ValueError("Incomplete comparison camera/settings identity")
    return camera, settings, actual["observation_mode"], actual["observation_profile_id"]
