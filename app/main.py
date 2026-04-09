import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.webhook import router as webhook_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="Agente Financeiro",
    description="Agente de IA para finanças pessoais e empresariais via WhatsApp",
    version="1.0.0",
)

# Webhook é server-to-server — CORS restrito à URL da Evolution API
_evolution_url = os.getenv("EVOLUTION_API_URL", "")
_allowed_origins = [_evolution_url] if _evolution_url else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["POST"],
    allow_headers=["*"],
)

app.include_router(webhook_router)


@app.get("/")
def root():
    return {
        "app": "Agente Financeiro",
        "status": "online",
        "version": "1.0.0",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}
