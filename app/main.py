"""
main.py — Punto de entrada de la aplicación FastAPI.

Para desarrollo local:
    uvicorn app.main:app --reload --port 8000

Para Render (Start Command):
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
    (Render inyecta $PORT automáticamente)
"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine, Base

# Importar modelos para que SQLAlchemy los registre en Base.metadata
import app.models  # noqa: F401

# Importar routers
from app.routers import factura as factura_router

# ── Crear tablas automáticamente al iniciar ─────────────────────────────────
# En producción se recomienda Alembic. Esto es útil para el primer deploy en Render.
try:
    Base.metadata.create_all(bind=engine)
    print("[DB] Tablas verificadas/creadas correctamente.")
except Exception as e:
    print(f"[DB] Advertencia al inicializar tablas: {e}")

# ── Instancia de la aplicación ──────────────────────────────────────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "API REST para facturación electrónica ecuatoriana integrada con el SRI. "
        "Soporta generación de XML, firma XAdES-BES, transmisión SOAP y RIDE en PDF."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    contact={
        "name": "Soporte POS SRI Ecuador",
        "email": "soporte@tuempresa.ec"
    }
)

# ── CORS (ajustar origins para producción) ──────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ⚠️ En producción, reemplazar con el dominio de tu app Flutter
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Registrar routers ───────────────────────────────────────────────────────
app.include_router(factura_router.router, prefix=settings.API_V1_STR)


# ── Health check ────────────────────────────────────────────────────────────
@app.get("/", tags=["General"], summary="Health check")
def health_check():
    """Verifica que la API está en línea. Usado por Render para health checks."""
    return {
        "status": "online",
        "app": settings.PROJECT_NAME,
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health", tags=["General"], summary="Health check detallado")
def health_detailed():
    """Health check detallado con información del ambiente."""
    from app.database import engine
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "error",
        "app": settings.PROJECT_NAME,
    }


# ── Entry point para ejecución directa ─────────────────────────────────────
# Render NO usa este bloque — usa el Start Command definido en el dashboard.
# Este bloque es útil solo para: python app/main.py (debugging local)
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=False,
        log_level="info"
    )
