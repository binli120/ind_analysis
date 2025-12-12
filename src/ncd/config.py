from pydantic import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:password@localhost:5432/inddb"
    llm_model_name: str = "gpt-5.1"
    llm_api_key: str = "YOUR_API_KEY"
    embedding_model_name: str = "text-embedding-3-large"

    class Config:
        env_file = ".env"


settings = Settings()
