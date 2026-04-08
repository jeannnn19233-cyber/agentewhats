import os
import sys
import traceback
import httpx
from fastapi import APIRouter, Request, HTTPException
from dotenv import load_dotenv
from app.agent import processar_mensagem
from app.vision import extrair_dados_boleto, formatar_boleto
from app import database as db

load_dotenv()

router = APIRouter()

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


async def enviar_mensagem(telefone: str, texto: str):
    """Envia mensagem de texto via Evolution API."""
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "number": telefone,
        "text": texto,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def enviar_botoes_sim_nao(telefone: str, texto: str) -> bool:
    """Envia mensagem com botões Sim/Não. Retorna True se enviou com sucesso."""
    url = f"{EVOLUTION_API_URL}/message/sendButtons/{EVOLUTION_INSTANCE}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "number": telefone,
        "title": "Confirmação",
        "description": texto,
        "footer": "Toque para responder",
        "buttons": [
            {"type": "reply", "displayText": "✅ Sim", "id": "confirmar_sim"},
            {"type": "reply", "displayText": "❌ Não", "id": "confirmar_nao"},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        print(f"[BOTOES] Enviados com sucesso para {telefone}", flush=True)
        return True
    except Exception as e:
        print(f"[BOTOES] Falhou ({e}) — fallback para texto", flush=True)
        return False


def extrair_telefone(data: dict) -> str:
    """Extrai número de telefone do payload da Evolution API."""
    # Evolution v2 format
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    # Remove @s.whatsapp.net
    return remote_jid.replace("@s.whatsapp.net", "").replace("@g.us", "")


def extrair_texto(data: dict) -> str:
    """Extrai texto da mensagem (inclusive cliques de botão)."""
    message = data.get("message", {})
    # Texto simples
    if "conversation" in message:
        return message["conversation"]
    # Texto em mensagem extendida
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text", "")
    # Resposta de botão (Baileys/Evolution): buttonsResponseMessage
    if "buttonsResponseMessage" in message:
        btn = message["buttonsResponseMessage"]
        button_id = btn.get("selectedButtonId", "")
        display = btn.get("selectedDisplayText", "")
        if button_id == "confirmar_sim":
            return "sim"
        if button_id == "confirmar_nao":
            return "não"
        return display or button_id
    # Resposta de template button
    if "templateButtonReplyMessage" in message:
        btn = message["templateButtonReplyMessage"]
        return btn.get("selectedDisplayText") or btn.get("selectedId", "")
    # Resposta de mensagem interativa (interactiveResponseMessage)
    if "interactiveResponseMessage" in message:
        ir = message["interactiveResponseMessage"]
        nm = ir.get("nativeFlowResponseMessage", {})
        params = nm.get("paramsJson", "")
        if "confirmar_sim" in params:
            return "sim"
        if "confirmar_nao" in params:
            return "não"
        return params
    # Legenda de imagem
    if "imageMessage" in message:
        return message["imageMessage"].get("caption", "")
    return ""


def extrair_imagem_url(data: dict) -> str | None:
    """Extrai URL da imagem se houver."""
    message = data.get("message", {})
    if "imageMessage" in message:
        # Evolution API armazena a media e fornece URL para download
        media_url = data.get("mediaUrl")
        if media_url:
            return media_url
        # Fallback: base64 message key para buscar via API
        msg_id = data.get("key", {}).get("id")
        if msg_id:
            return f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}/{msg_id}"
    return None


@router.post("/webhook")
async def webhook_evolution(request: Request):
    """Recebe webhooks da Evolution API (mensagens do WhatsApp)."""
    body = await request.json()
    print(f"[WEBHOOK] Payload recebido: {body}", flush=True)

    # Verifica o secret se configurado
    if WEBHOOK_SECRET:
        secret = request.headers.get("x-webhook-secret", "")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    event = body.get("event", "")

    # Só processa mensagens recebidas
    if event not in ("messages.upsert", "MESSAGES_UPSERT"):
        return {"status": "ignored", "event": event}

    data = body.get("data", {})

    # Ignora mensagens enviadas por nós
    if data.get("key", {}).get("fromMe", False):
        return {"status": "ignored", "reason": "own message"}

    telefone = extrair_telefone(data)
    if not telefone:
        return {"status": "error", "reason": "no phone number"}

    texto = extrair_texto(data)
    imagem_url = extrair_imagem_url(data)

    try:
        if imagem_url:
            # Processa imagem (boleto)
            dados_boleto = await extrair_dados_boleto(
                imagem_url, EVOLUTION_API_URL, EVOLUTION_API_KEY
            )
            resposta = formatar_boleto(dados_boleto)

            # Se veio com legenda pedindo para registrar, já registra
            if texto and any(p in texto.lower() for p in ["registra", "salva", "anota", "adiciona"]):
                if dados_boleto.valor and dados_boleto.vencimento:
                    # Converte vencimento DD/MM/AAAA -> AAAA-MM-DD
                    partes = dados_boleto.vencimento.split("/")
                    vencimento_iso = f"{partes[2]}-{partes[1]}-{partes[0]}" if len(partes) == 3 else dados_boleto.vencimento
                    db.criar_conta(
                        descricao=dados_boleto.descricao or "Boleto",
                        valor=dados_boleto.valor,
                        vencimento=vencimento_iso,
                        fornecedor=dados_boleto.beneficiario,
                    )
                    resposta += "\n\n✅ Conta registrada com sucesso!"

            # Salva conversa
            db.salvar_conversa(telefone, f"[IMAGEM] {texto}", resposta)
        elif texto:
            # Processa texto
            resposta = processar_mensagem(telefone, texto)
        else:
            return {"status": "ignored", "reason": "no content"}

        # Envia a resposta como texto (botões nativos do WhatsApp são bloqueados
        # pelo Meta para contas pessoais, então usamos texto sempre).
        await enviar_mensagem(telefone, resposta)

    except Exception as e:
        print(f"[WEBHOOK ERROR] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        error_msg = "Desculpe, tive um problema ao processar sua mensagem. Tente novamente em instantes."
        try:
            await enviar_mensagem(telefone, error_msg)
        except Exception as send_err:
            print(f"[WEBHOOK ERROR] Falha ao enviar fallback: {send_err}", flush=True)
        return {"status": "error", "detail": str(e)}

    return {"status": "ok"}
