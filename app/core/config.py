from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    anthropic_api_key: str = ""
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_jwt_secret: str = ""
    max_pdf_size_mb: int = 50
    llm_concurrency: int = 5
    fuzzy_threshold: float = 0.35
    fuzzy_confidence_promote: float = 0.60
    llm_writeback_threshold: float = 0.85

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

@lru_cache
def get_settings() -> Settings:
    return Settings()
