import json
import httpx
from openai import OpenAI
from app.prompts import VISION_PROMPT
from models.schemas import DadosBoleto

client = OpenAI()


async def baixar_imagem_evolution(image_url: str, api_url: str, api_key: str) -> bytes:
    """Baixa imagem do servidor da Evolution API."""
    headers = {"apikey": api_key}
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(image_url, headers=headers)
        resp.raise_for_status()
        return resp.content


async def extrair_dados_boleto(image_url: str, api_url: str, api_key: str) -> DadosBoleto:
    """Envia imagem para GPT-4o e extrai dados do boleto."""
    # Tenta usar a URL diretamente (se pública) ou baixa os bytes
    try:
        image_bytes = await baixar_imagem_evolution(image_url, api_url, api_key)
        import base64
        image_b64 = base64.b64encode(image_bytes).decode()
        image_content = {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        }
    except Exception:
        # Fallback: usa URL direta (funciona se a imagem for pública)
        image_content = {
            "type": "image_url",
            "image_url": {"url": image_url},
        }

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    image_content,
                ],
            }
        ],
        max_tokens=500,
    )

    texto = response.choices[0].message.content or "{}"

    # Limpa possíveis markers de code block
    texto = texto.strip()
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[-1]
    if texto.endswith("```"):
        texto = texto.rsplit("```", 1)[0]
    texto = texto.strip()

    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = {}

    return DadosBoleto(**dados)


def formatar_boleto(dados: DadosBoleto) -> str:
    """Formata dados do boleto para exibir no WhatsApp."""
    linhas = ["📄 *Dados do Boleto:*\n"]
    if dados.valor:
        linhas.append(f"💰 Valor: R$ {dados.valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    if dados.vencimento:
        linhas.append(f"📅 Vencimento: {dados.vencimento}")
    if dados.beneficiario:
        linhas.append(f"🏢 Beneficiário: {dados.beneficiario}")
    if dados.linha_digitavel:
        linhas.append(f"🔢 Linha digitável: {dados.linha_digitavel}")
    if dados.descricao:
        linhas.append(f"📝 Descrição: {dados.descricao}")
    linhas.append("\nDeseja que eu registre essa conta? ✅")
    return "\n".join(linhas)
