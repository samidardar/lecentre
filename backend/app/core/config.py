from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Toute la configuration passe par l'environnement (ou `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "CallWiz AI"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    api_prefix: str = "/api/v1"
    expose_unprefixed_routes: bool = True
    cors_origin_regex: str = (
        r"https://.*\.lovable\.app|https://.*\.lovableproject\.com|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?"
    )

    # --- Sécurité ---
    jwt_secret: str = "change-me-in-production-please-32-bytes-min"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 14
    login_rate_limit_per_minute: int = 10

    # --- Données ---
    database_url: str = "sqlite+aiosqlite:///./callwiz.db"
    redis_url: str | None = None
    data_dir: str = "./data"
    retention_days: int = 90

    # --- Voix / LLM (Gemini Live) ---
    live_provider: Literal["gemini", "mock"] = "mock"
    gemini_api_key: str | None = None
    gemini_live_model: str = "gemini-3.8-live"
    gemini_text_model: str = "gemini-3.5-flash-lite"
    gemini_default_voice: str = "Sulafat"
    default_language: str = "fr"
    live_language_lock: bool = True  # force la langue de l'organisation/campagne (sinon suit l'interlocuteur)
    live_send_language_code: bool = False  # envoie aussi language_code (fr-FR) si le modèle le supporte
    live_affective_dialog: bool = False  # adapte le ton à l'émotion de l'appelant (API v1beta)
    live_start_sensitivity: Literal["LOW", "HIGH"] = "HIGH"
    live_end_sensitivity: Literal["LOW", "HIGH"] = "HIGH"
    live_silence_duration_ms: int = 350
    live_prefix_padding_ms: int = 40

    # --- Télécom ---
    telephony_provider: Literal["twilio", "mock"] = "mock"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_default_from: str | None = None
    twilio_validate_signature: bool = True
    telephony_calls_per_second: float = 5.0

    # --- RAG ---
    vector_store: Literal["qdrant_local", "qdrant", "memory"] = "qdrant_local"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "callwiz_chunks"
    embedder: Literal["fastembed", "gemini", "hashing"] = "fastembed"
    fastembed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    gemini_embedding_model: str = "gemini-embedding-001"
    rag_top_k: int = 4
    rag_min_score: float = 0.012
    rag_enrich_with_llm: bool = False

    # --- Capacité ---
    global_max_concurrent_calls: int = 100
    default_max_call_seconds: int = 300

    # --- Silences (secondes) ---
    silence_nudge_s: float = 6.0
    silence_close_s: float = 14.0
    silence_hangup_s: float = 22.0

    # --- Coûts (EUR) ---
    cost_telephony_per_min: float = 0.0085
    cost_live_audio_in_per_min: float = 0.0035
    cost_live_audio_out_per_min: float = 0.0140
    cost_text_llm_per_call: float = 0.0004

    # --- MCP ---
    mcp_servers_json: str = Field(default="{}", description="{name: {command, args} | {url}}")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
