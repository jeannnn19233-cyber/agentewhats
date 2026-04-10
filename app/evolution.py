"""
Cliente assíncrono para a Evolution API.
Único ponto de envio de mensagens — importado por webhook.py e scheduler.py.
"""
import logging
import os
import httpx

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("EVOLUTION_API_URL", "")


def _instance() -> str:
    return os.getenv("EVOLUTION_INSTANCE", "")


def _headers() -> dict:
    return {
        "apikey": os.getenv("EVOLUTION_API_KEY", ""),
        "Content-Type": "application/json",
    }


async def enviar_mensagem(telefone: str, texto: str) -> None:
    """Envia mensagem de texto via Evolution API."""
    url = f"{_base_url()}/message/sendText/{_instance()}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, json={"number": telefone, "text": texto}, headers=_headers()
        )
        resp.raise_for_status()
    logger.debug("[%s] mensagem enviada (%d chars)", telefone, len(texto))


async def enviar_midia(telefone: str, image_b64: str, caption: str = "") -> None:
    """Envia imagem em base64 via Evolution API."""
    url = f"{_base_url()}/message/sendMedia/{_instance()}"
    payload = {
        "number": telefone,
        "mediatype": "image",
        "media": image_b64,
        "caption": caption,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
    logger.debug("[%s] imagem enviada (caption: %s)", telefone, caption[:60])


async def enviar_botoes(
    telefone: str,
    texto: str,
    botoes: list[dict],
    titulo: str = "",
    rodape: str = "",
) -> None:
    """Envia mensagem com botões clicáveis (WhatsApp Business).

    ``botoes`` — lista de dicts: [{"id": "btn1", "text": "Texto"}]
    Fallback: se o endpoint falhar, envia como texto simples.
    """
    url = f"{_base_url()}/message/sendButtons/{_instance()}"
    payload = {
        "number": telefone,
        "title": titulo,
        "description": texto,
        "footer": rodape,
        "buttons": [
            {
                "buttonId": b["id"],
                "buttonText": {"displayText": b["text"]},
                "type": 1,
            }
            for b in botoes
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
        logger.info("[%s] botões enviados (%d)", telefone, len(botoes))
    except Exception as e:
        logger.warning("[%s] falha ao enviar botões (%s) — fallback texto", telefone, e)
        # Fallback: monta texto com opções numeradas
        linhas = [texto, ""]
        for i, b in enumerate(botoes, 1):
            linhas.append(f"{i}. {b['text']}")
        if rodape:
            linhas.append(f"\n_{rodape}_")
        await enviar_mensagem(telefone, "\n".join(linhas))
