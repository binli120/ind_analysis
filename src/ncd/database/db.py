# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from sqlalchemy import create_engine
from sqlalchemy.exc import NoSuchModuleError
from sqlalchemy.orm import sessionmaker

from ncd.config.config import settings


def _create_engine(database_url: str):
    try:
        return create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,  # refresh stale Supabase / PgBouncer connections
        )
    except (ModuleNotFoundError, NoSuchModuleError) as exc:
        message = str(exc)
        if "psycopg" not in message:
            raise
        fallback_url = database_url.replace(
            "postgresql+psycopg://", "postgresql+psycopg2://"
        )
        if fallback_url == database_url:
            raise
        return create_engine(
            fallback_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
        )


engine = _create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
