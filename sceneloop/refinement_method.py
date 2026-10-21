"""Run-bound, replaceable descriptive method; never executable policy."""
import copy
import hashlib
from pathlib import Path

from .refinement_plan import RefinementError

RESOURCE = Path(__file__).with_name("resources") / "scene_refinement_method.md"


def bind_method(run):
    if "refinement_method" in run:
        return copy.deepcopy(run["refinement_method"])
    if run.get("usage", {}).get("brain_calls", 0):
        return {"schema_version": 1, "status": "legacy_unbound", "version": None,
                "sha256": None, "content": None, "source": None}
    try:
        content = RESOURCE.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise RefinementError("通用方法资源不可读。", "refinement_method_unavailable", 409) from error
    if not content.strip() or len(content.encode("utf-8")) > 65536:
        raise RefinementError("通用方法资源为空或超过64KiB。", "refinement_method_invalid", 409)
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"schema_version": 1, "status": "bound", "version": "sha256:" + sha,
            "sha256": sha, "content": content,
            "source": "sceneloop/resources/scene_refinement_method.md"}


def method_content(run):
    binding = run.get("refinement_method") or {}
    if binding.get("status") == "legacy_unbound":
        return ""
    content = binding.get("content")
    if (not isinstance(content, str) or
            hashlib.sha256(content.encode("utf-8")).hexdigest() != binding.get("sha256")):
        raise RefinementError("Run方法绑定缺失或损坏。", "refinement_method_invalid", 409)
    return content
