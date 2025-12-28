# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

try:
    from pydantic_settings import BaseSettings  # Pydantic v2 preferred location
    from pydantic import ConfigDict
except ImportError:  # pragma: no cover - fallback for environments without pydantic-settings
    from pydantic import BaseSettings
    try:  # pragma: no cover - pydantic v1 fallback
        from pydantic import ConfigDict
    except Exception:  # pragma: no cover - ConfigDict unavailable
        ConfigDict = None  # type: ignore[assignment]


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:password@localhost:5432/inddb"
    llm_model_name: str = "gpt-5.1"
    llm_api_key: str = "YOUR_API_KEY"
    embedding_model_name: str = "text-embedding-3-large"

    if ConfigDict:
        model_config = ConfigDict(env_file=".env")
    else:
        class Config:  # pragma: no cover - legacy pydantic fallback
            env_file = ".env"


settings = Settings()
