"""Membership rules engine.

The CRITERION is stored, not the individuals. Four types, closed list.
Several rules are UNIONed. Exclusions always win.
Attributes are won by the FIRST rule the user wrote that matches.
"""
import fnmatch
import re

TYPES = {
    "image":   "By image (accepts * )",
    "name":    "By name pattern (accepts * )",
    "label":   "By label key=value",
    "project": "By Compose project",
}


def _matches(rule, c):
    kind, value = rule.get("type"), rule.get("value", "")
    if kind == "image":
        return fnmatch.fnmatch(c["image"], value)
    if kind == "name":
        return fnmatch.fnmatch(c["name"], value) or fnmatch.fnmatch(c["target"], value)
    if kind == "project":
        return c["project"] == value
    if kind == "label":
        key = rule.get("key", "")
        return c["labels"].get(key) == value if value else key in c["labels"]
    return False


def evaluate(rules, exclusions, containers):
    """Containers that match, carrying the attributes of their first matching rule."""
    out = []
    for c in containers:
        if any(_matches(e, c) for e in exclusions or []):
            continue
        for i, r in enumerate(rules or []):
            if _matches(r, c):
                out.append({**c, "rule": i, **(r.get("attributes") or {})})
                break  # first match wins the attributes
    return out


def excluded_ids(exclusions, containers):
    """Ids of containers an exclusion rules out. They can never be picked."""
    return {c["id"] for c in containers
            if any(_matches(e, c) for e in exclusions or [])}


def propose(selected, everything):
    """From a hand-picked selection, propose the rules that cover it EXACTLY.

    Returns a list, never None for a non-empty selection: if no single criterion
    covers the selection and nothing more, it falls back to one name rule per
    container. Rules are a union, so N name rules match exactly what was ticked.

    Returning nothing here used to leave the wizard finishing with an empty rule
    and monitoring nothing, silently. That must not be possible.
    """
    if not selected:
        return []
    candidates = []
    # by common label
    common = set(selected[0]["labels"].items())
    for c in selected[1:]:
        common &= set(c["labels"].items())
    for key, value in sorted(common):
        candidates.append({"type": "label", "key": key, "value": value})
    # by common project
    projects = {c["project"] for c in selected}
    if len(projects) == 1:
        candidates.append({"type": "project", "value": projects.pop()})
    # by common image
    images = {c["image"] for c in selected}
    if len(images) == 1:
        candidates.append({"type": "image", "value": images.pop()})
    # by common name prefix
    names = [c["target"] for c in selected]
    prefix = names[0]
    for n in names[1:]:
        while prefix and not n.startswith(prefix):
            prefix = prefix[:-1]
    if len(prefix) >= 3:
        candidates.append({"type": "name", "value": prefix + "*"})

    selected_ids = {c["id"] for c in selected}
    # a single criterion, but only if it matches EXACTLY: no more, no less
    for cand in candidates:
        if {c["id"] for c in everything if _matches(cand, c)} == selected_ids:
            return [cand]

    # nothing generalises cleanly: name them one by one. Always exact, never silent.
    return [{"type": "name", "value": c["target"]} for c in selected]


_GENERATED_NAME = re.compile(r"^[a-z]+_[a-z]+$")


def unstable(containers):
    """Containers with no stable identity. The app warns; it does not block."""
    return [c for c in containers
            if c["identity_source"] == "name" and _GENERATED_NAME.match(c["name"])]
