from __future__ import annotations

from typing import Any


def evaluate_lighthouse_budgets(
    data: dict[str, Any], page_url: str, website: dict[str, Any]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    categories = data.get("categories") or {}
    audits = data.get("audits") or {}
    score_budgets = (
        ("performance", "Performance", int(website.get("budget_performance") or 70)),
        ("accessibility", "Accessibility", int(website.get("budget_accessibility") or 80)),
        ("seo", "SEO", int(website.get("budget_seo") or 80)),
    )
    for category_id, label, target in score_budgets:
        raw_score = (categories.get(category_id) or {}).get("score")
        if not isinstance(raw_score, (int, float)):
            continue
        current = round(raw_score * 100)
        if current < target:
            issues.append(budget_issue(
                f"budget-{category_id}", f"{label} score is below budget", page_url,
                f"Current {label} score: {current}/100. Required minimum: {target}/100.",
                max(1.0, min(10.0, (target - current) / 3)),
                f"Resolve the highest-impact {label.lower()} findings until this page reaches at least {target}/100.",
            ))

    metric_budgets = (
        ("largest-contentful-paint", "LCP", float(website.get("budget_lcp_ms") or 2500), "ms"),
        ("cumulative-layout-shift", "CLS", float(website.get("budget_cls") or 0.1), ""),
    )
    for audit_id, label, target, unit in metric_budgets:
        current = (audits.get(audit_id) or {}).get("numericValue")
        if not isinstance(current, (int, float)) or current <= target:
            continue
        current_label = f"{round(current)}ms" if unit == "ms" else f"{current:.3f}"
        target_label = f"{round(target)}ms" if unit == "ms" else f"{target:.3f}"
        action = (
            "Optimize the largest above-the-fold element, critical assets, server response, and render-blocking work."
            if label == "LCP" else
            "Reserve media dimensions and prevent content, fonts, banners, or widgets from shifting after initial render."
        )
        issues.append(budget_issue(
            f"budget-{audit_id}", f"{label} exceeds performance budget", page_url,
            f"Current {label}: {current_label}. Required maximum: {target_label}.",
            max(1.0, min(10.0, ((current / target) - 1) * 5)), action,
        ))
    return issues


def budget_issue(issue_id: str, title: str, page_url: str, explanation: str,
                 points: float, action: str) -> dict[str, Any]:
    return {
        "id": issue_id, "source": "budget", "title": title,
        "category": "Performance Budget", "difficulty": "High" if points >= 4 else "Medium",
        "responsibility": "Development / SEO", "occurrences": 1,
        "points": round(points, 1), "page_url": page_url,
        "affected_examples": [{
            "page_url": page_url, "selector": "", "snippet": "",
            "explanation": explanation, "recommendation": action,
        }],
    }
