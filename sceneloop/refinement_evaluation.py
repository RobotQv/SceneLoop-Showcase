"""Explicit item/Issue review protocol and pure historical projections."""
import copy
from .refinement_plan import digest, selection_identity

REVIEW_FIELDS = {"verdict", "evidence", "improved", "remaining", "expected_effect"}
OPTIONAL_FIELDS = {"item_results", "issue_reviews"}


def standards(plan):
    if not plan:
        return None
    items = []
    for item in plan["items"]:
        row = {k: copy.deepcopy(item[k]) for k in
               ("id", "desired", "basis", "criteria", "object_ids", "issue_ids")}
        row["criteria_digest"] = digest({k: row[k] for k in
                                         ("desired", "basis", "criteria", "object_ids")})
        items.append(row)
    return {"plan_id": plan["id"], "goal_id": plan["goal"]["id"], "items": items}


def review_schema(identity, verdicts):
    return {"type": "array", "maxItems": 128, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {identity: {"type": "string"},
            "verdict": {"type": "string", "enum": verdicts},
            "evidence": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
            **{key: {"type": "string", "maxLength": 2000}
               for key in ("improved", "remaining", "expected_effect")}},
        "required": [identity, *sorted(REVIEW_FIELDS)]}}


def validate_reviews(value, evidence_ids, frozen, issues):
    """No inference from aggregate verdict, omitted IDs, or text similarity."""
    items = {i["id"] for i in (frozen or {}).get("items", [])}
    issue_ids = {i["id"] for i in issues}
    for field, identity, allowed, verdicts in (
            ("item_results", "item_id", items, {"pass", "continue", "uncertain"}),
            ("issue_reviews", "issue_id", issue_ids, {"resolved", "remaining", "uncertain"})):
        rows = value.get(field, [])
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError("invalid review list")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != REVIEW_FIELDS | {identity}:
                raise ValueError("invalid review fields")
            key = row[identity]
            if not isinstance(key, str) or key not in allowed or key in seen:
                raise ValueError("unknown or duplicate review identity")
            seen.add(key)
            if row["verdict"] not in verdicts:
                raise ValueError("invalid review verdict")
            if any(not isinstance(row[k], str) or len(row[k]) > 2000
                   for k in ("improved", "remaining", "expected_effect")):
                raise ValueError("invalid review explanation")
            refs = row["evidence"]
            if (not isinstance(refs, list) or len(refs) > 32 or
                    any(not isinstance(e, str) or e not in evidence_ids for e in refs) or
                    row["verdict"] in {"pass", "resolved"} and not refs):
                raise ValueError("invalid review evidence")
    # Existing IDs are rechecked only through the explicit review protocol.
    if any(i.get("id") in issue_ids for i in value.get("issues", [])):
        raise ValueError("existing Issue requires issue_reviews")
    if len(issues) + len(value.get("issues", [])) > 128:
        raise ValueError("Issue ledger capacity exceeded; retain existing issues")
    ids = [i.get("id") for i in value.get("issues", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate new Issue")


def criteria_reason(frozen, current, *, item_ids=None, issue_ids=()):
    old = {i["id"]: i for i in (frozen or {}).get("items", [])}
    now = {i["id"]: i for i in (current or {}).get("items", [])}

    def linked_by(items):
        mapping = {}
        for item in items.values():
            for issue_id in item.get("issue_ids", []):
                mapping.setdefault(issue_id, set()).add(item["id"])
        return mapping

    selected = set(item_ids or [])
    old_links, now_links = linked_by(old), linked_by(now)
    for issue_id in issue_ids:
        # Each reviewed Issue keeps its own entry relations. Comparing only the
        # union of linked entries masks a swap between Issues that leaves the
        # union unchanged while both associations really changed, and it makes
        # the multi-Issue acceptance call disagree with the per-Issue view;
        # check every Issue on its own before folding it into the union.
        old_linked = old_links.get(issue_id, set())
        now_linked = now_links.get(issue_id, set())
        if old_linked != now_linked:
            return "linkage_changed"
        selected.update(old_linked)
        selected.update(now_linked)
    for key in selected:
        if key not in old or key not in now or old[key]["criteria_digest"] != now[key]["criteria_digest"]:
            return "criteria_changed"
    return None


def apply_reviews(workspace, judgement, timestamp):
    existing = {i["id"]: i for i in workspace["issues"]}
    for review in judgement.get("issue_reviews", []):
        issue = existing[review["issue_id"]]
        row = {**copy.deepcopy(review), "evaluation_id": judgement["evaluation_id"],
               "judgement_id": judgement["id"], "selection": copy.deepcopy(judgement["selection"]),
               "working_version": judgement["working_version"]}
        issue.setdefault("review_history", []).append(row)
        issue["status"] = "verified" if review["verdict"] == "resolved" else "pending"
        issue["updated_at"] = timestamp
        if review["verdict"] == "resolved":
            issue["verified_by"] = judgement["id"]
        else:
            issue.pop("verified_by", None)
    for discovery in judgement["issues"]:
        # A conflicting legacy new finding cannot overwrite an existing identity.
        if discovery["id"] in existing:
            continue
        existing[discovery["id"]] = {
            **copy.deepcopy(discovery), "source": "judge", "status": "pending",
            "working_version": judgement["working_version"], "updated_at": timestamp,
            "judgement_id": judgement["id"], "evaluation_id": judgement["evaluation_id"],
            "selection": copy.deepcopy(judgement["selection"])}
    workspace["issues"] = list(existing.values())


def project_evaluations(project, plan, records, verify):
    current = standards(plan)
    evaluations = []
    for receipt, manifest in records:
        result = receipt["result"]
        target = result.get("target") or {}
        frozen = (manifest.get("context") or {}).get("refinement_standards")
        reasons = []
        if receipt["application_status"] != "applied":
            reasons.append(receipt.get("application_reason") or receipt["application_status"])
        if target.get("kind") == "candidate":
            reasons.append("candidate_not_working")
        if selection_identity(target.get("current")) != selection_identity(project.get("current_scene")):
            reasons.append("scene_changed")
        if target.get("working_version") != project["workspace"]["working_version"]:
            reasons.append("working_version_changed")
        if target.get("stage") != project["workspace"]["stage"]:
            reasons.append("stage_changed")
        if plan and (not frozen or frozen.get("goal_id") != plan["goal"]["id"]):
            reasons.append("goal_changed")
        reasons.extend(verify(receipt, target))
        base = {"evaluation_id": receipt["evaluation_id"], "input_id": receipt["input_id"],
                "input_digest": receipt["input_digest"], "application_status": receipt["application_status"],
                "selection": copy.deepcopy(target.get("current")), "working_version": target.get("working_version"),
                "scope": copy.deepcopy(target.get("scope")), "provenance": "independent_judge",
                "result": copy.deepcopy(result), "current": not reasons, "reasons": list(dict.fromkeys(reasons))}
        for field, identity in (("item_results", "item_id"), ("issue_reviews", "issue_id")):
            base[field] = []
            for row in result.get(field, []):
                why = list(base["reasons"])
                changed = criteria_reason(frozen, current,
                    item_ids=[row[identity]] if field == "item_results" else [],
                    issue_ids=[row[identity]] if field == "issue_reviews" else [])
                if changed:
                    why.append(changed)
                base[field].append({**copy.deepcopy(row), "evaluation_id": receipt["evaluation_id"],
                                    "current": not why, "reasons": why})
        evaluations.append(base)
    projected_issues = []
    for issue in project["workspace"].get("issues", []):
        row = copy.deepcopy(issue)
        reviews = [r for e in evaluations for r in e["issue_reviews"] if r["issue_id"] == issue["id"]]
        current_reviews = [r for r in reviews if r["current"]]
        latest_current = current_reviews[-1] if current_reviews else None
        latest_any = reviews[-1] if reviews else None
        row["reviews"] = reviews
        row["current_resolved"] = bool(latest_current and latest_current["verdict"] == "resolved")
        row["review_required"] = not row["current_resolved"]
        if latest_current:
            row["applicability_reasons"] = list(latest_current["reasons"])
        elif latest_any:
            row["applicability_reasons"] = list(latest_any["reasons"])
        else:
            row["applicability_reasons"] = ["no_explicit_review"]
        if not latest_current and issue.get("working_version") != project["workspace"]["working_version"]:
            row["applicability_reasons"].append("working_version_changed")
        projected_issues.append(row)
    return {"schema_version": 1, "current_scene": selection_identity(project.get("current_scene")),
            "working_version": project["workspace"]["working_version"], "evaluations": evaluations,
            "issues": projected_issues, "unresolved_issues": [i for i in projected_issues if not i["current_resolved"]]}


def builder_feedback(view):
    """One current feedback view; historical review bodies stay in the audit."""
    result = {k: copy.deepcopy(view[k]) for k in
              ("schema_version", "current_scene", "working_version")}
    rows = view["evaluations"]
    applicable = [row for row in rows if row["current"]]
    latest = applicable[-1:]
    result["older_evaluations_available"] = max(0, len(rows) - len(latest))
    result["evaluations"] = [
        {**{k: copy.deepcopy(e[k]) for k in ("evaluation_id", "input_id", "application_status",
                                           "selection", "working_version", "scope", "provenance", "current", "reasons")},
         "verdict": e["result"].get("verdict"), "next_focus": e["result"].get("next_focus", []),
         "unknowns": e["result"].get("unknowns", [])} for e in latest]
    item_results = {}
    for evaluation in rows:
        for review in evaluation["item_results"]:
            if review["current"]:
                item_results[review["item_id"]] = copy.deepcopy(review)
    result["item_results"] = list(item_results.values())
    result["historical_evaluations"] = [{k: copy.deepcopy(row[k]) for k in
        ("evaluation_id", "selection", "working_version", "current", "reasons")} for row in rows[-4:] if not row["current"]]
    result["unresolved_issues"] = []
    for issue in view["unresolved_issues"]:
        row = {k: copy.deepcopy(v) for k, v in issue.items() if k not in {"review_history", "reviews"}}
        current = [review for review in issue["reviews"] if review.get("current")]
        reviews = current[-1:] or issue["reviews"][-1:]
        row["review"] = copy.deepcopy(reviews[0]) if reviews else None
        row["older_reviews_available"] = max(0, len(issue["reviews"]) - len(reviews))
        result["unresolved_issues"].append(row)
    return result
