import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

async def update_user():
    client = AsyncIOMotorClient('mongodb://localhost:27017')
    db = client.opshero
    result = await db.users.update_one(
        {'github_login': 'NICE-DEV226'},
        {'$set': {'tier': 'team', 'updated_at': datetime.utcnow()}}
    )
    print(f'Updated {result.modified_count} user(s)')
    client.close()

asyncio.run(update_user())