"""Seed script — creates one demo user if it doesn't already exist.

Run with:
    python seed.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.database import AsyncSessionLocal, init_db
from app.core.security import hash_password
from app.models.user import User

DEMO_EMAIL = "demo@personalassistant.local"
DEMO_NAME = "Demo User"
DEMO_PASSWORD = "demo1234"


async def seed() -> None:
    await init_db()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == DEMO_EMAIL))
        existing = result.scalar_one_or_none()

        if existing is not None:
            print(f"⏭️  Demo user already exists ({DEMO_EMAIL}) — skipping.")
            return

        user = User(
            email=DEMO_EMAIL,
            name=DEMO_NAME,
            hashed_password=hash_password(DEMO_PASSWORD),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        print(f"✅ Demo user created: {DEMO_EMAIL} / {DEMO_PASSWORD}  (id={user.id})")


if __name__ == "__main__":
    asyncio.run(seed())
