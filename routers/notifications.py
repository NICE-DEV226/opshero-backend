"""
Notifications router — in-app notifications system.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel

from database import get_db
from deps.auth import CurrentUser
from models.notification import Notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


# ── GET /notifications ────────────────────────────────────────────────────────

@router.get("/")
async def list_notifications(
    user: CurrentUser,
    unread_only: bool = Query(False, description="Only return unread notifications"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """List user's notifications."""
    db = get_db()
    
    query = {"user_id": user.id}
    if unread_only:
        query["read"] = False
    
    # Remove expired notifications
    await db.notifications.delete_many({
        "expires_at": {"$lt": datetime.utcnow()}
    })
    
    skip = (page - 1) * per_page
    total = await db.notifications.count_documents(query)
    
    cursor = (
        db.notifications.find(query, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
    )
    
    items = await cursor.to_list(per_page)
    unread_count = await db.notifications.count_documents({
        "user_id": user.id,
        "read": False
    })
    
    return {
        "items": items,
        "total": total,
        "unread_count": unread_count,
        "page": page,
        "per_page": per_page,
    }


# ── POST /notifications/{notification_id}/read ───────────────────────────────

@router.post("/{notification_id}/read")
async def mark_as_read(notification_id: str, user: CurrentUser):
    """Mark a notification as read."""
    db = get_db()
    
    result = await db.notifications.update_one(
        {"id": notification_id, "user_id": user.id},
        {"$set": {"read": True}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    
    return {"ok": True}


# ── POST /notifications/read-all ──────────────────────────────────────────────

@router.post("/read-all")
async def mark_all_as_read(user: CurrentUser):
    """Mark all notifications as read."""
    db = get_db()
    
    result = await db.notifications.update_many(
        {"user_id": user.id, "read": False},
        {"$set": {"read": True}}
    )
    
    return {"ok": True, "marked_count": result.modified_count}


# ── DELETE /notifications/{notification_id} ───────────────────────────────────

@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, user: CurrentUser):
    """Delete a notification."""
    db = get_db()
    
    result = await db.notifications.delete_one({
        "id": notification_id,
        "user_id": user.id
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    
    return {"ok": True}


# ── Helper functions ──────────────────────────────────────────────────────────

async def create_notification(
    user_id: str,
    type: str,
    title: str,
    message: str,
    data: dict = None,
    expires_at: datetime = None
) -> str:
    """Create a new notification for a user."""
    db = get_db()
    
    notification = Notification(
        user_id=user_id,
        type=type,
        title=title,
        message=message,
        data=data or {},
        expires_at=expires_at
    )
    
    await db.notifications.insert_one(notification.model_dump())
    logger.info(f"Created notification {type} for user {user_id}")
    
    return notification.id


async def create_quota_warning_notification(user_id: str, analyses_used: int, limit: int) -> str:
    """Create a quota warning notification."""
    percentage = int((analyses_used / limit) * 100)
    return await create_notification(
        user_id=user_id,
        type="quota_warning",
        title=f"Quota Warning: {percentage}% Used",
        message=f"You've used {analyses_used} of {limit} analyses this month ({percentage}%)",
        data={
            "analyses_used": analyses_used,
            "limit": limit,
            "percentage": percentage
        }
    )


async def create_quota_exhausted_notification(user_id: str, limit: int) -> str:
    """Create a quota exhausted notification."""
    return await create_notification(
        user_id=user_id,
        type="quota_exhausted",
        title="Monthly Limit Reached",
        message=f"You've used all {limit} analyses for this month. Upgrade to continue.",
        data={"limit": limit}
    )


async def create_analysis_shared_notification(user_id: str, analysis_id: str, shared_by: str) -> str:
    """Create a notification when an analysis is shared with the user."""
    return await create_notification(
        user_id=user_id,
        type="analysis_shared",
        title="Analysis Shared With You",
        message=f"{shared_by} shared an analysis result with you",
        data={
            "analysis_id": analysis_id,
            "shared_by": shared_by
        }
    )


async def create_system_notification(user_id: str, title: str, message: str, notification_type: str = "system_maintenance") -> str:
    """Create a system notification (maintenance, updates, etc.)."""
    return await create_notification(
        user_id=user_id,
        type=notification_type,
        title=title,
        message=message,
        data={}
    )


async def create_feedback_reply_notification(
    user_id: str, 
    feedback_title: str, 
    admin_reply: str = None, 
    new_status: str = None,
    feedback_id: str = None
) -> str:
    """Create a notification when admin replies to user feedback."""
    if admin_reply:
        title = f"Admin replied to your feedback"
        message = f"An admin has responded to your feedback '{feedback_title}'. Check your submissions to see the reply."
    elif new_status in ["planned", "done"]:
        status_labels = {"planned": "planned for development", "done": "completed"}
        title = f"Your feedback has been {status_labels[new_status]}"
        message = f"Good news! Your feedback '{feedback_title}' has been {status_labels[new_status]}."
    else:
        title = f"Update on your feedback"
        message = f"Your feedback '{feedback_title}' has been updated. Check your submissions for details."
    
    return await create_notification(
        user_id=user_id,
        type="feedback_reply",
        title=title,
        message=message,
        data={
            "feedback_id": feedback_id,
            "feedback_title": feedback_title,
            "admin_reply": admin_reply,
            "new_status": new_status
        }
    )


async def create_pattern_approved_notification(user_id: str, pattern_name: str, pattern_id: str) -> str:
    """Create a notification when user's contributed pattern is approved."""
    return await create_notification(
        user_id=user_id,
        type="pattern_approved",
        title="Your pattern contribution was approved!",
        message=f"Congratulations! Your pattern '{pattern_name}' has been approved and added to the library.",
        data={
            "pattern_id": pattern_id,
            "pattern_name": pattern_name
        }
    )


async def create_pattern_rejected_notification(user_id: str, pattern_name: str, reason: str = None) -> str:
    """Create a notification when user's contributed pattern is rejected."""
    message = f"Your pattern contribution '{pattern_name}' needs some changes before it can be approved."
    if reason:
        message += f" Reason: {reason}"
    
    return await create_notification(
        user_id=user_id,
        type="pattern_rejected",
        title="Pattern contribution needs changes",
        message=message,
        data={
            "pattern_name": pattern_name,
            "rejection_reason": reason
        }
    )


async def create_new_feedback_notification(admin_user_ids: list, feedback_title: str, author_github: str, feedback_id: str) -> None:
    """Create notifications for admins when new feedback is submitted."""
    for admin_id in admin_user_ids:
        try:
            await create_notification(
                user_id=admin_id,
                type="new_feedback",
                title="New user feedback received",
                message=f"@{author_github} submitted new feedback: '{feedback_title}'",
                data={
                    "feedback_id": feedback_id,
                    "feedback_title": feedback_title,
                    "author_github": author_github
                }
            )
        except Exception as e:
            logger.warning(f"Failed to create feedback notification for admin {admin_id}: {e}")