from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import settings

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def get_db() -> AsyncIOMotorDatabase:
    if db is None:
        raise RuntimeError("Database is not ready.")
    return db


async def initialise_database() -> None:
    """Connect to the fixed MongoDB database named subtitle and migrate harmless defaults."""
    global client, db
    if not settings.database_uri:
        raise RuntimeError("DATABASE is missing.")

    client = AsyncIOMotorClient(settings.database_uri, serverSelectionTimeoutMS=12_000)
    db = client.get_database("subtitle")
    await db.command("ping")

    await db.members.create_index("username", unique=True, sparse=True)
    await db.members.create_index("telegram_id", unique=True, sparse=True)
    await db.titles.create_index([("tmdb_type", 1), ("tmdb_id", 1)], unique=True, sparse=True)
    await db.titles.create_index([("status", 1), ("updated_at", -1)])
    await db.subtitles.create_index([("channel_id", 1), ("message_id", 1)], unique=True)
    await db.subtitles.create_index([("title_id", 1), ("status", 1), ("created_at", -1)])
    await db.subtitles.create_index([("uploader_id", 1), ("updated_at", -1)])
    await db.episode_pages.create_index([("title_id", 1), ("season", 1), ("episode", 1)], unique=True)
    await db.episode_pages.create_index([("title_id", 1), ("updated_at", -1)])
    await db.votes.create_index([("title_id", 1), ("visitor_id", 1)], unique=True)
    await db.comments.create_index([("title_id", 1), ("created_at", -1)])
    await db.comment_votes.create_index([("comment_id", 1), ("visitor_id", 1)], unique=True)
    await db.reports.create_index([("status", 1), ("created_at", -1)])
    await db.bot_drafts.create_index("expires_at", expireAfterSeconds=0)
    await db.bot_drafts.create_index([("user_id", 1), ("updated_at", -1)])
    await db.bot_states.create_index("expires_at", expireAfterSeconds=0)
    await db.bot_states.create_index("user_id", unique=True)

    owner = await db.members.find_one({"telegram_id": str(settings.owner_id)})
    owner_payload = {
        "telegram_id": str(settings.owner_id),
        "role": "owner",
        "active": True,
        "updated_at": utcnow(),
    }
    if owner:
        await db.members.update_one({"_id": owner["_id"]}, {"$set": owner_payload})
    else:
        await db.members.insert_one(
            {
                "_id": new_id("member_"),
                "username": None,
                "display_name": "Owner",
                "avatar_url": "",
                "bio": "",
                "joined_at": utcnow(),
                **owner_payload,
            }
        )


async def close_database() -> None:
    global client, db
    if client:
        client.close()
    client = None
    db = None


def json_safe(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result = dict(document)
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result
