"""
Project model for team collaboration.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from uuid import uuid4


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    
    # Basic info
    name: str
    description: Optional[str] = None
    team_id: str
    
    # Repository info
    github_repo: Optional[str] = None  # "owner/repo"
    default_branch: str = "main"
    
    # Settings
    is_active: bool = True
    auto_analyze: bool = True  # Auto-analyze CI/CD failures
    
    # Members (subset of team members assigned to this project)
    members: List[str] = Field(default_factory=list)  # user_ids
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str  # user_id of creator


class ProjectMember(BaseModel):
    user_id: str
    github_login: str
    github_avatar_url: Optional[str] = None
    github_name: Optional[str] = None
    role: str = "member"  # member | lead
    joined_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectStats(BaseModel):
    total_analyses: int = 0
    analyses_this_week: int = 0
    avg_confidence: float = 0.0
    success_rate: float = 0.0
    top_error_categories: List[dict] = Field(default_factory=list)
    recent_activity: List[dict] = Field(default_factory=list)