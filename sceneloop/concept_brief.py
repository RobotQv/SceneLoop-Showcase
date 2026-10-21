"""Pure ConceptBrief validation and prompt compilation; no I/O or provider state."""
from __future__ import annotations

import hashlib
import json

from . import prompts

SCHEMA_VERSION = 1
TEMPLATE_VERSION = "concept-brief-v1"
BRIEF_FIELDS = ("schema_version", "scene", "composition", "key_objects", "details")
SCENE_LIMIT = 600
COMPOSITION_LIMIT = 600
OBJECT_LIMIT = 160
OBJECTS_MIN = 1
OBJECTS_MAX = 12
DETAILS_MIN = 0
DETAILS_MAX = 12
USER_PROMPT_LIMIT = 16000
STYLE_LIMIT = 4000
PRESERVE_LIMIT = 4000
PROMPT_LIMIT_MIN = 1
PROMPT_LIMIT_MAX = 24000

_MESSAGES = {
    "invalid_brief": "ConceptBrief does not match the frozen field contract.",
    "invalid_source": "The original request, style or preserve field is invalid.",
    "invalid_prompt_limit": "prompt_limit must be an integer within 1..24000.",
    "prompt_too_long": "The compiled prompt exceeds the given limit.",
}


class ConceptBriefError(ValueError):
    """Fixed, non-echoing failure for ConceptBrief validation and compilation."""

    def __init__(self, code):
        super().__init__(_MESSAGES[code])
        self.code = code


def _fail(code):
    raise ConceptBriefError(code)


def _ensure_utf8(value, code):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(code)


def _text(value, limit, code, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit:
        _fail(code)
    if not allow_empty and not value.strip():
        _fail(code)
    _ensure_utf8(value, code)
    return value


def _string_list(value, minimum, maximum, item_limit, code):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(code)
    return [_text(item, item_limit, code) for item in value]


def validate_concept_brief(value):
    """Return an independent copy of a strictly shaped, parsed ConceptBrief dict."""
    if not isinstance(value, dict) or set(value) != set(BRIEF_FIELDS):
        _fail("invalid_brief")
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _fail("invalid_brief")
    return {
        "schema_version": SCHEMA_VERSION,
        "scene": _text(value["scene"], SCENE_LIMIT, "invalid_brief"),
        "composition": _text(value["composition"], COMPOSITION_LIMIT, "invalid_brief"),
        "key_objects": _string_list(value["key_objects"], OBJECTS_MIN, OBJECTS_MAX, OBJECT_LIMIT, "invalid_brief"),
        "details": _string_list(value["details"], DETAILS_MIN, DETAILS_MAX, OBJECT_LIMIT, "invalid_brief"),
    }


def compile_concept_brief(brief, *, user_prompt, style, preserve, prompt_limit):
    """Validate inputs and join the frozen instructions with one JSON data segment."""
    validated = validate_concept_brief(brief)
    source = {
        "场景需求": _text(user_prompt, USER_PROMPT_LIMIT, "invalid_source"),
        "设计意图与风格": _text(style, STYLE_LIMIT, "invalid_source", allow_empty=True),
        "保留项与功能要求": _text(preserve, PRESERVE_LIMIT, "invalid_source", allow_empty=True),
    }
    if isinstance(prompt_limit, bool) or not isinstance(prompt_limit, int) or not PROMPT_LIMIT_MIN <= prompt_limit <= PROMPT_LIMIT_MAX:
        _fail("invalid_prompt_limit")
    data = dict(source)
    data["ConceptBrief"] = validated
    final_prompt = prompts.CONCEPT_INSTRUCTIONS + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(final_prompt) > prompt_limit:
        _fail("prompt_too_long")
    source_json = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    return {
        "schema_version": SCHEMA_VERSION,
        "brief": validated,
        "final_prompt": final_prompt,
        "prompt_sha256": hashlib.sha256(final_prompt.encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(source_json.encode("utf-8")).hexdigest(),
        "template_version": TEMPLATE_VERSION,
        "base_template_version": prompts.PROMPT_TEMPLATE_VERSION,
        "prompt_chars": len(final_prompt),
        "prompt_limit": prompt_limit,
    }
