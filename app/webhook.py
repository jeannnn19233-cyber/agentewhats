import logging
import os
from fastapi import APIRouter, Request, HTTPException
from dotenv import load_dotenv
from app.agent import processar_mensagem
from app.vision import extrair_dados_boleto, formatar_boleto
from app import database as db
from app.evolution import enviar_mensagem, enviar_midia
from models.schemas import AgentResponse

logger = logging.getLogger(__name__)

load_dotenv()

router = APIRouter()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


async def _enviar_resposta(telefone: str, resultado: AgentResponse | str):
    """Despacha texto, imagem ou os dois conforme o tipo de resposta."""
    if isinstance(resultado, str):
        await enviar_mensagem(telefone, resultado)
        return

    if resultado.image_b64:
        await enviar_midia(telefone, resultado.image_b64, resultado.image_caption)
    if resultado.text:
        await enviar_mensagem(telefone, resultado.text)


def extrair_telefone(data: dict) -> str:
    """Extrai número de telefone do payload da Evolution API."""
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    return remote_jid.replace("@s.whatsapp.net", "").replace("@g.us", "")


# Emojis tratados como confirmação positiva (👍 = SIM)
EMOJIS_SIM = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
              "✅", "✔️", "❤️", "🔥", "💯", "👌", "👌🏻", "👌🏼", "👌🏽", "👌🏾", "👌🏿"}

# Emojis tratados como negação (👎 = NÃO)
EMOJIS_NAO = {"👎", "👎🏻", "👎🏼", "👎🏽", "👎🏾", "👎🏿", "❌", "🚫", "✖️"}


def extrair_texto(data: dict) -> str:
    """Extrai texto da mensagem (inclusive reactions emoji)."""
    message = data.get("message", {})

    if "reactionMessage" in message:
        emoji = (message["reactionMessage"] or {}).get("text", "")
        if not emoji:
            return ""
        if emoji in EMOJIS_SIM:
            return "sim"
        if emoji in EMOJIS_NAO:
            return "não"
        return ""

    if "conversation" in message:
        return message["conversation"]
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text", "")
    if "imageMessage" in message:
        return message["imageMessage"].get("caption", "")
    return ""


def extrair_imagem_url(data: dict) -> str | None:
    """Extrai URL da imagem se houver."""
    message = data.get("message", {})
    if "imageMessage" in message:
        media_url = data.get("mediaUrl")
        if media_url:
            return media_url
        msg_id = data.get("key", {}).get("id")
        if msg_id:
            return f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}/{msg_id}"
    return None


@router.post("/webhook")
async def webhook_evolution(request: Request):
    """Recebe webhooks da Evolution API (mensagens do WhatsApp)."""
    body = await request.json()
    logger.debug("Payload recebido: %s", body)

    if WEBHOOK_SECRET:
        secret = request.headers.get("x-webhook-secret", "")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    event = body.get("event", "")
    if event not in ("messages.upsert", "MESSAGES_UPSERT"):
        return {"status": "ignored", "event": event}

    data = body.get("data", {})
    if data.get("key", {}).get("fromMe", False):
        return {"status": "ignored", "reason": "own message"}

    telefone = extrair_telefone(data)
    if not telefone:
        return {"status": "error", "reason": "no phone number"}

    texto = extrair_texto(data)
    imagem_url = extrair_imagem_url(data)

    try:
        if imagem_url:
            # Processa imagem (boleto) — sempre vai para pending_action, nunca salva direto
            dados_boleto = await extrair_dados_boleto(
                imagem_url, EVOLUTION_API_URL, EVOLUTION_API_KEY
            )
            resposta_txt = formatar_boleto(dados_boleto)

            if dados_boleto.valor and dados_boleto.vencimento:
                partes = dados_boleto.vencimento.split("/")
                vencimento_iso = (
                    f"{partes[2]}-{partes[1]}-{partes[0]}"
                    if len(partes) == 3
                    else dados_boleto.vencimento
                )
                db.criar_pending_action(
                    telefone=telefone,
                    action_type="criar_conta",
                    action_data={
                        "descricao": dados_boleto.descricao or "Boleto",
                        "valor": dados_boleto.valor,
                        "vencimento": vencimento_iso,
                        "fornecedor": dados_boleto.beneficiario,
                    },
                    preview=resposta_txt,
                )
                resposta_txt += (
                    "\n\n━━━━━━━━━━━━━━━\n*Posso registrar?*\n\n"
                    "✅ *SIM* — confirmar (ou reaja com 👍)\n"
                    "❌ *NÃO* — cancelar (ou reaja com 👎)\n"
                    "━━━━━━━━━━━━━━━"
                )

            db.salvar_conversa(telefone, f"[IMAGEM] {texto}", resposta_txt)
            resultado: AgentResponse | str = resposta_txt

        elif texto:
            resultado = processar_mensagem(telefone, texto)
        else:
            return {"status": "ignored", "reason": "no content"}

        await _enviar_resposta(telefone, resultado)

    except Exception as e:
        logger.error("[%s] erro no processamento: %s", telefone, e, exc_info=True)
        try:
            await enviar_mensagem(
                telefone,
                "Desculpe, tive um problema ao processar sua mensagem. Tente novamente em instantes.",
            )
        except Exception as send_err:
            logger.error("[%s] falha ao enviar mensagem de erro: %s", telefone, send_err)
        return {"status": "error", "detail": str(e)}

    return {"status": "ok"}
