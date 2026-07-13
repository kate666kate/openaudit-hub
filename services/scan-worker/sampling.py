from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse


def route_template(url: str) -> str:
    segments = [segment for segment in urlparse(str(url or "")).path.split("/") if segment]
    if not segments:
        return "/"
    first = normalize_segment(segments[0])
    if len(segments) == 1:
        return f"/{first}"
    return f"/{first}/:page"


def normalize_segment(value: str) -> str:
    value = str(value or "").strip().lower()
    if re.fullmatch(r"\d+|[a-f0-9]{12,}|[0-9a-f-]{24,}", value):
        return ":id"
    return value or ":page"


def select_lighthouse_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    limit = max(1, int(limit or 1))
    ordered = sorted(
        candidates,
        key=lambda page: (
            int(page.get("depth") or 0),
            0 if page.get("source") == "homepage" else 1,
            len(str(page.get("url") or "")),
            str(page.get("url") or ""),
        ),
    )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in ordered:
        groups[route_template(str(page.get("url") or ""))].append(page)

    group_order = sorted(groups, key=lambda group: (0 if group == "/" else 1, group))
    selected: list[dict[str, Any]] = []
    round_index = 0
    while len(selected) < limit:
        added = False
        for group in group_order:
            pages = groups[group]
            if round_index >= len(pages):
                continue
            selected.append({**pages[round_index], "sample_group": group})
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
        round_index += 1
    return selected
