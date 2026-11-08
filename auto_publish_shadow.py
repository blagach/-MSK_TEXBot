"""Fail-closed shadow-mode decision for future automatic publication.

This module NEVER publishes or sends messages. Manual review remains the
only operational route until a separate explicitly authorized rollout.
"""
from dataclasses import dataclass
from lead_moderation import assess

@dataclass(frozen=True)
class ShadowResult:
    route: str
    reason: str

def decide(*, enabled: bool = False, approved: bool, duplicate: bool,
           recent_requests: int, category: str, region: str, area: str,
           place: str, work_date: str, details: str) -> ShadowResult:
    result = assess(
        previously_approved=approved, duplicate=duplicate,
        recent_requests=recent_requests, category=category, region=region,
        area=area, place=place, work_date=work_date, details=details,
    )
    if not enabled:
        return ShadowResult("manual", "Автоматический режим выключен")
    if result.status != "eligible":
        return ShadowResult("manual", result.reason)
    # Shadow-only: candidate status is informational, not a publish command.
    return ShadowResult("candidate", "Подходит для автопубликации после отдельного разрешения")
