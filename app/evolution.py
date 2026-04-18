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


_MAX_WHATSAPP_CHARS = 4000  # WhatsApp corta em ~4096; margem de segurança


def _split_texto(texto: str, limite: int = _MAX_WHATSAPP_CHARS) -> list[str]:
    """Divide texto longo em partes respeitando quebras de linha."""
    if len(texto) <= limite:
        return [texto]
    partes = []
    while texto:
        if len(texto) <= limite:
            partes.append(texto)
            break
        # Tenta cortar na última quebra de linha antes do limite
        pos = texto.rfind("\n", 0, limite)
        if pos == -1 or pos < limite // 2:
            # Sem quebra boa — corta no limite
            pos = limite
        partes.append(texto[:pos])
        texto = texto[pos:].lstrip("\n")
    return partes


async def enviar_mensagem(telefone: str, texto: str) -> None:
    """Envia mensagem de texto via Evolution API. Divide automaticamente se exceder limite do WhatsApp."""
    url = f"{_base_url()}/message/sendText/{_instance()}"
    partes = _split_texto(texto)
    async with httpx.AsyncClient(timeout=30) as client:
        for i, parte in enumerate(partes):
            resp = await client.post(
                url, json={"number": telefone, "text": parte}, headers=_headers()
            )
            resp.raise_for_status()
    logger.debug("[%s] mensagem enviada (%d chars, %d partes)", telefone, len(texto), len(partes))


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
    Tenta 2 formatos de payload. Se ambos falharem, envia como texto.
    """
    headers = _headers()
    instance = _instance()
    base = _base_url()

    # --- Formato 1: Evolution API v2 (flat) ---
    payload_v2 = {
        "number": telefone,
        "title": titulo or " ",
        "description": texto,
        "footer": rodape or " ",
        "buttons": [
            {"type": "reply", "displayText": b["text"], "id": b["id"]}
            for b in botoes
        ],
    }

    # --- Formato 2: Baileys legacy ---
    payload_legacy = {
        "number": telefone,
        "title": titulo or " ",
        "description": texto,
        "footerText": rodape or " ",
        "buttons": [
            {
                "buttonId": b["id"],
                "buttonText": {"displayText": b["text"]},
                "type": 1,
            }
            for b in botoes
        ],
    }

    url = f"{base}/message/sendButtons/{instance}"

    for label, payload in [("v2", payload_v2), ("legacy", payload_legacy)]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                # Verifica se a API rejeitou silenciosamente
                if body.get("error") or body.get("status") == "error":
                    logger.warning("[%s] sendButtons %s rejeitado: %s", telefone, label, body)
                    continue
            logger.info("[%s] botões enviados via formato %s (%d)", telefone, label, len(botoes))
            return  # sucesso — sai da função
        except Exception as e:
            logger.warning("[%s] sendButtons %s falhou: %s", telefone, label, e)
            continue

    # --- Fallback: texto com opções numeradas (sempre funciona) ---
    logger.info("[%s] fallback texto para botões", telefone)
    await enviar_mensagem(telefone, texto)
