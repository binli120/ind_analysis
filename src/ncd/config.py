# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

try:
    from pydantic_settings import BaseSettings  # Pydantic v2 preferred location
except ImportError:  # pragma: no cover - fallback for environments without pydantic-settings
    from pydantic import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:password@localhost:5432/inddb"
    llm_model_name: str = "gpt-5.1"
    llm_api_key: str = "YOUR_API_KEY"
    embedding_model_name: str = "text-embedding-3-large"

    class Config:
        env_file = ".env"


settings = Settings()
