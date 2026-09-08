from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.config import settings

_SETTINGS_PATH = Path(__file__).resolve().parent / "agent_settings.yaml"
_CACHE: "AgentSettings | None" = None
_CACHE_MTIME: float | None = None


class TimingConfig(BaseModel):
    silence_trigger_enabled: bool = True
    silence_threshold_seconds: int = 120
    analysis_interval_minutes: int = 5
    warmup_minutes: int = 3
    agent_cooldown_seconds: int = 5
    room_auto_intervention_cooldown_seconds: int = 180
    rule_trigger_marker_ttl_seconds: int = 180
    agent_response_timeout_seconds: int = 90
    agent_global_concurrency_limit: int = 3
    mention_entry_enabled: bool = False
    mention_entry_rate_per_sec: int = 3
    mention_entry_queue_max_wait_sec: int = 60
    time_progress_jitter_enabled: bool = True
    time_progress_jitter_min_seconds: int = 30
    time_progress_jitter_max_seconds: int = 90
    emotional_support_enabled: bool = True
    emotional_support_check_interval_seconds: int = 30
    emotional_support_keywords: list[str] = Field(
        default_factory=lambda: [
            "不会",
            "不知道",
            "太难",
            "好难",
            "算了",
            "烦",
            "好烦",
            "没人说",
            "随便",
            "不想",
            "没意思",
            "做不下去",
            "卡住",
            "放弃",
        ]
    )
    emotional_support_recent_window_seconds: int = 120
    emotional_support_cooldown_seconds: int = 180


class ThresholdsConfig(BaseModel):
    monopoly_message_count: int = 5


class MentionConfig(BaseModel):
    enabled: bool = True
    priority: int = 0
    max_mentions_per_message: int = 1


class AutoSpeakConfig(BaseModel):
    facilitator_silence_enabled: bool = True
    time_progress_enabled: bool = True
    committee_enabled: bool = True
    committee_recent_same_role_window: int = 2
    monopoly_encourager_enabled: bool = True
    committee_devil_advocate_enabled: bool = True
    committee_summarizer_enabled: bool = True
    committee_encourager_enabled: bool = True


class BailianSearchAppConfig(BaseModel):
    enabled: bool = False
    app_id_env: str = "BAILIAN_SEARCH_APP_ID"
    timeout_seconds: int = 120


class ModelConfigItem(BaseModel):
    model_version: str = settings.AGENT_MODEL


class ModelsConfig(BaseModel):
    role_agents: ModelConfigItem = Field(default_factory=ModelConfigItem)


class AgentSettings(BaseModel):
    timing: TimingConfig = Field(default_factory=TimingConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    mention: MentionConfig = Field(default_factory=MentionConfig)
    auto_speak: AutoSpeakConfig = Field(default_factory=AutoSpeakConfig)
    bailian_search_app: BailianSearchAppConfig = Field(default_factory=BailianSearchAppConfig)


def get_agent_settings(force_reload: bool = False) -> AgentSettings:
    global _CACHE, _CACHE_MTIME

    try:
        current_mtime = _SETTINGS_PATH.stat().st_mtime
    except FileNotFoundError:
        current_mtime = None

    if (
        not force_reload
        and _CACHE is not None
        and _CACHE_MTIME is not None
        and current_mtime == _CACHE_MTIME
    ):
        return _CACHE

    data = {}
    if _SETTINGS_PATH.exists():
        data = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}

    _CACHE = AgentSettings(**data)
    _CACHE_MTIME = current_mtime
    return _CACHE

