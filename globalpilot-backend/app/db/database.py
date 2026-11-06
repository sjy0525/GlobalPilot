from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine=create_engine(settings.database_url,echo=True)

sessionLocal=sessionmaker(autocommit=False,autoflush=False,bind=engine)


def async_database_url(database_url: str) -> URL:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("GlobalPilot requires PostgreSQL for its asynchronous database")
    # SQLAlchemy selects psycopg's AsyncConnection when create_async_engine is used.
    # Keep credentials, query parameters and database name from the configured URL.
    return url.set(drivername="postgresql+psycopg")


async_engine = create_async_engine(async_database_url(settings.database_url))
async_sessionLocal = async_sessionmaker(
    bind=async_engine,
    autoflush=False,
    expire_on_commit=False,
)


async def get_async_db() -> AsyncIterator[AsyncSession]:
    # Every request owns one Session; callers explicitly commit their writes.
    async with async_sessionLocal() as db:
        yield db

def get_db():
    db=sessionLocal()
    try:
        yield db
    finally:
        db.close()
