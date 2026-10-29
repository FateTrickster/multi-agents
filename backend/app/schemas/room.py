from uuid import UUID
from typing import Optional
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.models.room import RoomStatus


class RoomCreate(BaseModel):
    name: str
    task_id: Optional[UUID] = None
    agent_mode: Literal["none", "single", "multi"] = "multi"


class RoomUpdate(BaseModel):
    status: RoomStatus


class RoomDeleteRequest(BaseModel):
    confirm_name: str


class RoomTaskBindRequest(BaseModel):
    task_id: UUID


class TaskScriptConfirmRequest(BaseModel):
    current_status: Optional[str] = None
    next_goal: Optional[str] = None
    student_feedback: Optional[str] = None
    proposal_id: Optional[str] = None
    lease_id: Optional[str] = None


class TaskScriptLeaseRequest(BaseModel):
    lease_id: str


class RoomActivityRequest(BaseModel):
    activity_type: Literal["chat", "writing", "task_edit"] = "writing"


class WritingSubmitStateResponse(BaseModel):
    required_confirmations: int = 1
    confirmations: list[dict] = Field(default_factory=list)
    final_submitted_at: Optional[datetime] = None
    action: Optional[str] = None


class WritingDocStateResponse(BaseModel):
    content: str = ""
    version: int = 0
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    updated_by_display_name: Optional[str] = None


class WritingDocChangeLogItem(BaseModel):
    action: str = "update"
    at: Optional[datetime] = None
    actor_id: Optional[str] = None
    actor_display_name: Optional[str] = None
    version: int = 0
    summary: str = ""
    delta_chars: int = 0


class WritingDocChangeLogResponse(BaseModel):
    items: list[WritingDocChangeLogItem] = Field(default_factory=list)


class RoomResponse(BaseModel):
    id: UUID
    name: str
    task_id: Optional[UUID]
    created_by: UUID
    status: RoomStatus
    member_count: int = 0
    student_count: int = 0
    online_count: int = 0
    timer_started_at: Optional[datetime] = None
    timer_deadline_at: Optional[datetime] = None
    timer_stopped_at: Optional[datetime] = None
    locked_member_ids: Optional[list[str]] = None
    agent_mode: Literal["none", "single", "multi"] = "multi"

    model_config = ConfigDict(from_attributes=True)
