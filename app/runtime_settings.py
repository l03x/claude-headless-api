"""Admin-editable settings, stored as JSON rows in `app_settings` with code-side defaults."""

from typing import Any

from sqlalchemy.orm import Session

from app.models import AppSetting

EFFORTS = ["low", "medium", "high", "xhigh", "max"]

DEFAULTS: dict[str, Any] = {
    "default_model": "sonnet",
    "default_effort": "medium",
    "enabled_models": [
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ],
    "request_timeout_s": 300,
    "max_concurrency": 4,
    "default_rate_limit_rpm": 60,
    "log_bodies": False,
    "log_retention_days": 30,
    "session_retention_days": 0,
    "default_system_prompt": "You are a helpful assistant.",
    "max_budget_usd_per_request": None,
    # Lets requests opt in to Claude Code's WebSearch tool (web_search_options). Off: no tools at all.
    "web_search_enabled": False,
}


def get_all(db: Session) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in db.query(AppSetting).all():
        if row.key in DEFAULTS:
            values[row.key] = row.value
    return values


def get(db: Session, key: str) -> Any:
    row = db.get(AppSetting, key)
    return row.value if row is not None else DEFAULTS[key]


def set_many(db: Session, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    db.commit()


def effort_rank(effort: str) -> int:
    return EFFORTS.index(effort)
