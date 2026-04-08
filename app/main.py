import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.webhook import router as webhook_router

load_dotenv()

app = FastAPI(
    title="Agente Financeiro",
    description="Agente de IA para finanças pessoais e empresariais via WhatsApp",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
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
