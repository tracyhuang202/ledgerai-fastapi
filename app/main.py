from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import bills, rules, clients
from app.core.config import get_settings

cfg = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("/tmp/taxtech").mkdir(parents=True, exist_ok=True)
    yield

app = FastAPI(
    title="LedgerAI API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills.router,   prefix="/api/v1")
app.include_router(rules.router,   prefix="/api/v1")
app.include_router(clients.router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}