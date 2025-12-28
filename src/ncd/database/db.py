# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ncd.config.config import settings

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
