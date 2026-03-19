"""
Notification model for in-app notifications.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from uuid import uuid4


class Notification(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    type: str  # "team_invite", "team_accepted", "analysis_shared", etc.
    title: str
    message: str
    data: dict = Field(default_factory=dict)  # Extra data (team_id, invite_token, etc.)
    read: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None