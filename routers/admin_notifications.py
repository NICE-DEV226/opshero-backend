"""
Admin notifications router — send system notifications to users.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel

from database import get_db
from deps.admin_auth import AdminUser
from routers.notifications import create_system_notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/notifications", tags=["admin-notifications"])


class SystemNotificationRequest(BaseModel):
    title: str
    message: str
    type: str = "system_maintenance"  # system_maintenance, feature_announcement, security_alert
    target_tiers: list[str] = ["free", "pro", "team"]  # Which tiers to notify
    expires_hours: Optional[int] = 24  # Hours until notification expires


@router.post("/system")
async def send_system_notification(
    body: SystemNotificationRequest,
    admin: AdminUser,
):
    """Send a system notification to all users or specific tiers."""
    db = get_db()
    
    # Build user query based on target tiers
    user_query = {"is_active": True, "is_suspended": False}
    if body.target_tiers and len(body.target_tiers) < 3:  # Not all tiers
        user_query["tier"] = {"$in": body.target_tiers}
    
    # Get all target users
    users = await db.users.find(user_query, {"id": 1}).to_list(None)
    
    if not users:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No users found matching criteria")
    
    # Calculate expiration
    expires_at = None
    if body.expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=body.expires_hours)
    
    # Send notification to all target users
    notification_count = 0
    for user in users:
        try:
            await create_system_notification(
                user_id=user["id"],
                title=body.title,
                message=body.message,
                notification_type=body.type
            )
            notification_count += 1
        except Exception as e:
            logger.error(f"Failed to create notification for user {user['id']}: {e}")
    
    logger.info(f"Admin {admin.username} sent system notification to {notification_count} users")
    
    return {
        "ok": True,
        "notifications_sent": notification_count,
        "target_users": len(users),
        "message": f"System notification sent to {notification_count} users"
    }


@router.get("/stats")
async def get_notification_stats(admin: AdminUser):
    """Get notification statistics."""
    db = get_db()
    
    # Count notifications by type
    pipeline = [
        {"$group": {
            "_id": "$type",
            "count": {"$sum": 1},
            "unread_count": {"$sum": {"$cond": [{"$eq": ["$read", False]}, 1, 0]}}
        }},
        {"$sort": {"count": -1}}
    ]
    
    type_stats = await db.notifications.aggregate(pipeline).to_list(None)
    
    # Total stats
    total_notifications = await db.notifications.count_documents({})
    total_unread = await db.notifications.count_documents({"read": False})
    
    # Recent notifications
    recent = await db.notifications.find(
        {},
        {"_id": 0, "user_id": 1, "type": 1, "title": 1, "created_at": 1, "read": 1}
    ).sort("created_at", -1).limit(10).to_list(10)
    
    return {
        "total_notifications": total_notifications,
        "total_unread": total_unread,
        "by_type": type_stats,
        "recent_notifications": recent
    }