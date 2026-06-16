import os
from dotenv import load_dotenv

# Cargar .env en desarrollo local (en Render las vars se inyectan directamente)
load_dotenv()


class Settings:
    # ── Proyecto ────────────────────────────────────────────────────────────────
    PROJECT_NAME: str = "API POS SRI Ecuador"
    API_V1_STR: str = "/api"

    # ── Servidor ────────────────────────────────────────────────────────────────
    # Render inyecta PORT automáticamente. En local usamos 8000.
    PORT: int = int(os.getenv("PORT", "8000"))

    # ── Base de Datos ────────────────────────────────────────────────────────────
    # Render inyecta DATABASE_URL con esquema "postgres://..." (psycopg2 necesita
    # "postgresql://..."), así que corregimos el prefijo si es necesario.
    _raw_db_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///./local_dev.db"          # Fallback: SQLite para desarrollo local
    )

    @property
    def DATABASE_URL(self) -> str:
        url = self._raw_db_url
        # Render usa el prefijo "postgres://" (antiguo), SQLAlchemy 2.x necesita
        # "postgresql://" — corregimos automáticamente.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    # ── Certificado Digital (ruta configurable por variable de entorno) ──────────
    # En Render: configura CERT_P12_PATH apuntando al path del archivo subido
    # (p.ej. usando Render Disk o codificado en base64 como CERT_P12_B64).
    CERT_P12_PATH: str = os.getenv("CERT_P12_PATH", "certificados/10928234_0604426908.p12")
    CERT_P12_PASSWORD: str = os.getenv("CERT_P12_PASSWORD", "Byron12")

    # ── Almacenamiento ──────────────────────────────────────────────────────────
    STORAGE_DIR: str = os.getenv("STORAGE_DIR", "storage")


settings = Settings()

# Crear directorio de almacenamiento si no existe (local)
os.makedirs(settings.STORAGE_DIR, exist_ok=True)
