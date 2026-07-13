from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def attach_visual_evidence(
    reports_dir: Path,
    website_key: str,
    url: str,
    source: str,
    issues: list[dict[str, Any]],
) -> None:
    limit = max(0, min(10, int(os.getenv("EVIDENCE_SCREENSHOT_LIMIT", "5"))))
    if not limit:
        return
    selectors: list[str] = []
    for issue in issues:
        for example in issue.get("affected_examples") or []:
            selector = str(example.get("selector") or "").strip()
            if selector and selector not in selectors:
                selectors.append(selector)
            if len(selectors) >= limit:
                break
        if len(selectors) >= limit:
            break
    if not selectors:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    prefix = reports_dir / f"evidence-{website_key}-{source}-{stamp}"
    encoded = base64.urlsafe_b64encode(json.dumps(selectors).encode("utf-8")).decode("ascii").rstrip("=")
    try:
        result = subprocess.run(
            ["node", "/app/capture-evidence.js", url, str(prefix), encoded],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode != 0:
            return
        captures = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return
    by_selector = {str(item.get("selector") or ""): item for item in captures}
    for issue in issues:
        for example in issue.get("affected_examples") or []:
            capture = by_selector.get(str(example.get("selector") or ""))
            if not capture:
                continue
            example["screenshot_path"] = Path(str(capture.get("screenshot") or "")).name
            example["highlight"] = json.dumps(capture.get("rect") or {}, separators=(",", ":"))
