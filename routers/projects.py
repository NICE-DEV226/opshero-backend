"""
Projects router — team project management.

Projects are sub-groups within teams focused on specific repositories/services.
All analyses are linked to projects, enabling better organization and metrics.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel

from database import get_db
from deps.auth import CurrentUser
from models.project import Project, ProjectMember, ProjectStats
from models.user import TIER_LIMITS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_user_team(user_id: str, db) -> Optional[dict]:
    """Return the team document where user is a member."""
    return await db.teams.find_one(
        {"members.user_id": user_id},
        {"_id": 0},
    )


async def _check_team_permission(user_id: str, team_id: str, required_role: str = "member") -> bool:
    """Check if user has required role in team."""
    db = get_db()
    team = await db.teams.find_one({"id": team_id})
    if not team:
        return False
    
    user_member = next((m for m in team.get("members", []) if m["user_id"] == user_id), None)
    if not user_member:
        return False
    
    role_hierarchy = {"member": 0, "admin": 1, "owner": 2}
    user_level = role_hierarchy.get(user_member["role"], 0)
    required_level = role_hierarchy.get(required_role, 0)
    
    return user_level >= required_level


# ── GET /projects/team ────────────────────────────────────────────────────────

@router.get("/team")
async def list_team_projects(user: CurrentUser):
    """List all projects in user's team."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")
    
    # Get all projects for this team
    projects = await db.projects.find(
        {"team_id": team_doc["id"], "is_active": True},
        {"_id": 0}
    ).sort("created_at", -1).to_list(None)
    
    # Enrich with member info and stats
    enriched_projects = []
    for project in projects:
        # Get member details
        member_ids = project.get("members", [])
        members = []
        if member_ids:
            users = await db.users.find(
                {"id": {"$in": member_ids}},
                {"_id": 0, "id": 1, "github_login": 1, "github_avatar_url": 1, "github_name": 1}
            ).to_list(None)
            members = [
                ProjectMember(
                    user_id=u["id"],
                    github_login=u["github_login"],
                    github_avatar_url=u.get("github_avatar_url"),
                    github_name=u.get("github_name"),
                    role="member"  # TODO: Add project-specific roles
                ) for u in users
            ]
        
        # Get basic stats
        total_analyses = await db.analyses.count_documents({"project_id": project["id"]})
        week_ago = datetime.utcnow() - timedelta(days=7)
        analyses_this_week = await db.analyses.count_documents({
            "project_id": project["id"],
            "created_at": {"$gte": week_ago}
        })
        
        enriched_projects.append({
            **project,
            "members": [m.model_dump() for m in members],
            "member_count": len(members),
            "total_analyses": total_analyses,
            "analyses_this_week": analyses_this_week
        })
    
    return {"projects": enriched_projects}


# ── POST /projects ────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str
    description: Optional[str] = None
    github_repo: Optional[str] = None
    members: List[str] = []  # user_ids to add to project


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProjectRequest, user: CurrentUser):
    """Create a new project in user's team."""
    if user.tier != "team":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Projects require the 'team' tier."
        )
    
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")
    
    # Check permission (admin or owner can create projects)
    if not await _check_team_permission(user.id, team_doc["id"], "admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only team admins and owners can create projects."
        )
    
    # Validate members are in the team
    team_member_ids = [m["user_id"] for m in team_doc.get("members", [])]
    invalid_members = [m for m in body.members if m not in team_member_ids]
    if invalid_members:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"These users are not in your team: {invalid_members}"
        )
    
    project = Project(
        name=body.name.strip(),
        description=body.description.strip() if body.description else None,
        team_id=team_doc["id"],
        github_repo=body.github_repo.strip() if body.github_repo else None,
        members=body.members,
        created_by=user.id
    )
    
    await db.projects.insert_one(project.model_dump())
    
    logger.info("Project '%s' created by user %s in team %s", 
                project.name, user.github_login, team_doc["id"])
    
    return {
        "project_id": project.id,
        "name": project.name,
        "team_id": project.team_id
    }


# ── GET /projects/{project_id} ────────────────────────────────────────────────

@router.get("/{project_id}")
async def get_project(project_id: str, user: CurrentUser):
    """Get project details with stats."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check user is in the team
    team_doc = await _get_user_team(user.id, db)
    if not team_doc or team_doc["id"] != project["team_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied.")
    
    # Get detailed stats
    total_analyses = await db.analyses.count_documents({"project_id": project_id})
    week_ago = datetime.utcnow() - timedelta(days=7)
    analyses_this_week = await db.analyses.count_documents({
        "project_id": project_id,
        "created_at": {"$gte": week_ago}
    })
    
    # Average confidence
    pipeline = [
        {"$match": {"project_id": project_id}},
        {"$group": {"_id": None, "avg_confidence": {"$avg": "$confidence"}}}
    ]
    confidence_result = await db.analyses.aggregate(pipeline).to_list(1)
    avg_confidence = confidence_result[0]["avg_confidence"] if confidence_result else 0.0
    
    # Top error categories
    category_pipeline = [
        {"$match": {"project_id": project_id, "detected_category": {"$ne": None}}},
        {"$group": {"_id": "$detected_category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 5}
    ]
    categories = await db.analyses.aggregate(category_pipeline).to_list(5)
    
    # Recent activity (last 10 analyses)
    recent = await db.analyses.find(
        {"project_id": project_id},
        {"_id": 0, "id": 1, "pattern_id": 1, "confidence": 1, "created_at": 1, "user_id": 1}
    ).sort("created_at", -1).limit(10).to_list(10)
    
    # Enrich recent activity with user info
    if recent:
        user_ids = list(set(a["user_id"] for a in recent))
        users = await db.users.find(
            {"id": {"$in": user_ids}},
            {"_id": 0, "id": 1, "github_login": 1}
        ).to_list(None)
        user_map = {u["id"]: u["github_login"] for u in users}
        
        for activity in recent:
            activity["user_github_login"] = user_map.get(activity["user_id"], "unknown")
    
    return {
        **project,
        "stats": {
            "total_analyses": total_analyses,
            "analyses_this_week": analyses_this_week,
            "avg_confidence": avg_confidence,
            "top_error_categories": [
                {"category": cat["_id"], "count": cat["count"]} 
                for cat in categories
            ],
            "recent_activity": recent
        }
    }


# ── GET /projects/{project_id}/analyses ──────────────────────────────────────

@router.get("/{project_id}/analyses")
async def get_project_analyses(
    project_id: str, 
    user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None),
    pattern_id: Optional[str] = Query(None),
    confidence_min: Optional[float] = Query(None, ge=0, le=1),
):
    """Get analyses for a specific project."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check user is in the team
    team_doc = await _get_user_team(user.id, db)
    if not team_doc or team_doc["id"] != project["team_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied.")
    
    # Build query
    query = {"project_id": project_id}
    if category:
        query["detected_category"] = category
    if pattern_id:
        query["pattern_id"] = pattern_id
    if confidence_min is not None:
        query["confidence"] = {"$gte": confidence_min}
    
    skip = (page - 1) * per_page
    total = await db.analyses.count_documents(query)
    
    # Get analyses with user info
    pipeline = [
        {"$match": query},
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": per_page},
        {
            "$lookup": {
                "from": "users",
                "localField": "user_id",
                "foreignField": "id",
                "as": "user_info"
            }
        },
        {
            "$addFields": {
                "user_github_login": {"$arrayElemAt": ["$user_info.github_login", 0]},
                "user_github_avatar": {"$arrayElemAt": ["$user_info.github_avatar_url", 0]}
            }
        },
        {"$unset": ["user_info", "_id"]}
    ]
    
    analyses = await db.analyses.aggregate(pipeline).to_list(per_page)
    
    return {
        "analyses": analyses,
        "total": total,
        "page": page,
        "per_page": per_page,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "description": project.get("description")
        }
    }


# ── PUT /projects/{project_id}/members ────────────────────────────────────────

class UpdateMembersRequest(BaseModel):
    members: List[str]  # user_ids


@router.put("/{project_id}/members")
async def update_project_members(
    project_id: str, 
    body: UpdateMembersRequest, 
    user: CurrentUser
):
    """Update project members."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check permission
    if not await _check_team_permission(user.id, project["team_id"], "admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only team admins and owners can manage project members."
        )
    
    # Validate all members are in the team
    team = await db.teams.find_one({"id": project["team_id"]})
    team_member_ids = [m["user_id"] for m in team.get("members", [])]
    invalid_members = [m for m in body.members if m not in team_member_ids]
    if invalid_members:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"These users are not in your team: {invalid_members}"
        )
    
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"members": body.members, "updated_at": datetime.utcnow()}}
    )
    
    return {"ok": True, "members_updated": len(body.members)}


# ── POST /projects/{project_id}/auto-assign ──────────────────────────────────

class AutoAssignRequest(BaseModel):
    analysis_ids: List[str]
    reason: Optional[str] = None


@router.post("/{project_id}/auto-assign")
async def auto_assign_analyses(
    project_id: str,
    body: AutoAssignRequest,
    user: CurrentUser
):
    """Auto-assign existing analyses to a project."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check permission
    if not await _check_team_permission(user.id, project["team_id"], "admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only team admins and owners can assign analyses to projects."
        )
    
    # Validate analyses belong to team members
    team = await db.teams.find_one({"id": project["team_id"]})
    team_member_ids = [m["user_id"] for m in team.get("members", [])]
    
    # Update analyses
    result = await db.analyses.update_many(
        {
            "id": {"$in": body.analysis_ids},
            "user_id": {"$in": team_member_ids},
            "project_id": {"$in": [None, ""]}  # Only unassigned analyses
        },
        {
            "$set": {
                "project_id": project_id,
                "assigned_at": datetime.utcnow(),
                "assigned_by": user.id,
                "assignment_reason": body.reason
            }
        }
    )
    
    logger.info(
        f"User {user.github_login} assigned {result.modified_count} analyses to project {project['name']}"
    )
    
    return {
        "assigned_count": result.modified_count,
        "message": f"Assigned {result.modified_count} analyses to project {project['name']}"
    }


# ── GET /projects/{project_id}/suggestions ───────────────────────────────────

@router.get("/{project_id}/suggestions")
async def get_assignment_suggestions(project_id: str, user: CurrentUser):
    """Get suggested analyses that could be assigned to this project."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check user is in the team
    team_doc = await _get_user_team(user.id, db)
    if not team_doc or team_doc["id"] != project["team_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied.")
    
    suggestions = []
    
    # Get team member IDs
    team_member_ids = [m["user_id"] for m in team_doc.get("members", [])]
    
    # Suggestion 1: Analyses from project members without project assignment
    unassigned_query = {
        "user_id": {"$in": team_member_ids},
        "project_id": {"$in": [None, ""]},
        "created_at": {"$gte": datetime.utcnow() - timedelta(days=30)}
    }
    
    # If project has GitHub repo, prioritize analyses with matching context
    if project.get("github_repo"):
        repo_analyses = await db.analyses.find({
            **unassigned_query,
            "$or": [
                {"metadata.gh_repo": project["github_repo"]},
                {"context.gh_repo": project["github_repo"]},
                {"extracted_vars.repository": project["github_repo"]}
            ]
        }, {"_id": 0}).limit(10).to_list(10)
        
        if repo_analyses:
            suggestions.append({
                "type": "github_repo_match",
                "title": f"Analyses from {project['github_repo']}",
                "description": f"Found {len(repo_analyses)} analyses that appear to be from the project repository",
                "analyses": repo_analyses,
                "confidence": "high"
            })
    
    # Suggestion 2: Recent analyses from project members
    recent_analyses = await db.analyses.find(
        unassigned_query,
        {"_id": 0}
    ).sort("created_at", -1).limit(15).to_list(15)
    
    if recent_analyses:
        suggestions.append({
            "type": "recent_team_analyses",
            "title": "Recent team member analyses",
            "description": f"Recent analyses from team members that could belong to this project",
            "analyses": recent_analyses,
            "confidence": "medium"
        })
    
    # Suggestion 3: Analyses with similar error patterns
    if project.get("total_analyses", 0) > 0:
        # Get common patterns from existing project analyses
        pattern_pipeline = [
            {"$match": {"project_id": project_id}},
            {"$group": {"_id": "$pattern_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5}
        ]
        
        common_patterns = await db.analyses.aggregate(pattern_pipeline).to_list(5)
        pattern_ids = [p["_id"] for p in common_patterns if p["_id"]]
        
        if pattern_ids:
            similar_analyses = await db.analyses.find({
                "user_id": {"$in": team_member_ids},
                "project_id": {"$in": [None, ""]},
                "pattern_id": {"$in": pattern_ids},
                "created_at": {"$gte": datetime.utcnow() - timedelta(days=60)}
            }, {"_id": 0}).limit(10).to_list(10)
            
            if similar_analyses:
                suggestions.append({
                    "type": "similar_patterns",
                    "title": "Similar error patterns",
                    "description": f"Analyses with error patterns commonly seen in this project",
                    "analyses": similar_analyses,
                    "confidence": "medium"
                })
    
    return {"suggestions": suggestions}


# ── DELETE /projects/{project_id} ─────────────────────────────────────────────

@router.delete("/{project_id}")
async def delete_project(project_id: str, user: CurrentUser):
    """Delete a project (soft delete)."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    
    # Check permission (only owner or creator can delete)
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied.")
    
    my_role = next((m["role"] for m in team_doc["members"] if m["user_id"] == user.id), None)
    if my_role != "owner" and project["created_by"] != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only team owner or project creator can delete projects."
        )
    
    # Soft delete
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )
    
    logger.info("Project %s deleted by user %s", project_id, user.github_login)
    return {"ok": True}