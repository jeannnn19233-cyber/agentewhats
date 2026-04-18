import json
import logging
import os
import re
from datetime import date
import httpx
from openai import OpenAI
from dotenv import load_dotenv
from app.prompts import SYSTEM_PROMPT, INTENT_PROMPT
from app import database as db
from models.schemas import AgentResponse

load_dotenv()

logger = logging.getLogger(__name__)
client = OpenAI()
MODEL_CLASSIFICACAO = "gpt-4o-mini"   # rápido e barato — usado para classificar intenção
MODEL_RESPOSTA = "gpt-4o-mini"        # rápido e barato — qualidade excelente para WhatsApp


# ============================================================
# Helpers
# ============================================================

def _formatar_historico(historico: list[dict]) -> str:
    """Formata o histórico de conversas como texto para o prompt."""
    if not historico:
        return "(sem histórico — primeira mensagem do usuário)"
    linhas = []
    for h in historico:
        msg = h.get("mensagem", "").strip()
        resp = h.get("resposta", "").strip()
        linhas.append(f"Usuário: {msg}\nMaria: {resp}")
    return "\n\n".join(linhas)


def _formatar_valor(v) -> str:
    try:
        return f"R$ {float(v):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(v)


# ============================================================
# Classificação de intenção (com contexto de pending)
# ============================================================

def classificar_intencao(mensagem: str, historico: list[dict],
                         pending: dict | None = None,
                         usuario: dict | None = None) -> dict:
    """Classifica a intenção do usuário usando GPT, com contexto de pending_action e onboarding."""
    historico_txt = _formatar_historico(historico)

    # Contexto ativo: onboarding + pending action
    partes_ctx = []
    if usuario and not usuario.get("onboarding_completo"):
        partes_ctx.append(
            f"ONBOARDING NÃO COMPLETO. Dados atuais do cliente: "
            f"nome={usuario.get('nome') or '?'}, tipo={usuario.get('tipo') or '?'}, "
            f"cnpj={usuario.get('cnpj') or 'não informado'}."
        )
    else:
        partes_ctx.append("Onboarding completo.")

    if pending:
        partes_ctx.append(
            f"Há uma ação pendente de confirmação — "
            f"tipo: {pending['action_type']}, dados: {json.dumps(pending['action_data'], ensure_ascii=False)}"
        )
    else:
        partes_ctx.append("Nenhuma ação pendente no momento.")

    contexto_ativo = " ".join(partes_ctx)

    prompt = (
        INTENT_PROMPT
        .replace("{contexto_ativo}", contexto_ativo)
        .replace("{historico}", historico_txt)
        .replace("{mensagem}", mensagem)
    )

    response = client.chat.completions.create(
        model=MODEL_CLASSIFICACAO,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0,
    )
    texto = (response.choices[0].message.content or "{}").strip()
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[-1]
    if texto.endswith("```"):
        texto = texto.rsplit("```", 1)[0]
    texto = texto.strip()

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        logger.warning("Falha ao parsear JSON de classificação. Resposta: %r", texto)
        return {"intencao": "outro", "dados": {}}


def consultar_cnpj(cnpj: str) -> dict | None:
    """Consulta CNPJ na API pública da ReceitaWS. Retorna dados ou None."""
    cnpj_limpo = re.sub(r'\D', '', cnpj)
    if len(cnpj_limpo) != 14:
        return None
    try:
        resp = httpx.get(f"https://receitaws.com.br/v1/cnpj/{cnpj_limpo}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") != "ERROR":
                return {
                    "cnpj": cnpj_limpo,
                    "razao_social": data.get("nome", ""),
                    "fantasia": data.get("fantasia", ""),
                    "situacao": data.get("situacao", ""),
                    "atividade": data.get("atividade_principal", [{}])[0].get("text", ""),
                }
    except Exception:
        pass
    return None


def _formatar_perfil(usuario: dict) -> str:
    """Formata o perfil do usuário como contexto para o LLM."""
    partes = []
    if usuario.get("nome"):
        partes.append(f"Nome: {usuario['nome']}")
    partes.append(f"Tipo de uso: {usuario.get('tipo', 'pessoal')}")
    if usuario.get("razao_social"):
        partes.append(f"Empresa: {usuario['razao_social']}")
    if usuario.get("faixa_salarial"):
        partes.append(f"Faixa salarial: {usuario['faixa_salarial']}")
    if usuario.get("faturamento"):
        partes.append(f"Faturamento mensal: {usuario['faturamento']}")
    if usuario.get("orcamento_mensal"):
        partes.append(f"Orçamento mensal: {_formatar_valor(usuario['orcamento_mensal'])}")
    return "\n".join(partes)


def gerar_resposta(mensagem: str, historico: list[dict],
                   contexto: str = "", usuario: dict | None = None) -> str:
    """Gera resposta conversacional usando o histórico recente como memória."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if usuario:
        perfil_txt = _formatar_perfil(usuario)
        if perfil_txt:
            messages.append({"role": "system", "content": f"Perfil do usuário:\n{perfil_txt}"})

    for h in historico:
        msg = (h.get("mensagem") or "").strip()
        resp = (h.get("resposta") or "").strip()
        if msg:
            messages.append({"role": "user", "content": msg})
        if resp:
            messages.append({"role": "assistant", "content": resp})

    if contexto:
        messages.append({"role": "system", "content": f"Contexto adicional:\n{contexto}"})

    messages.append({"role": "user", "content": mensagem})

    response = client.chat.completions.create(
        model=MODEL_RESPOSTA,
        messages=messages,
        max_tokens=800,
        temperature=0.4,
    )
    return response.choices[0].message.content or "Desculpe, não consegui processar sua mensagem."


# ============================================================
# Execução de ações pendentes (após confirmação)
# ============================================================

def executar_pending_action(telefone: str, action: dict) -> str:
    """Executa uma ação que estava aguardando confirmação."""
    tipo = action.get("action_type")
    dados = action.get("action_data") or {}

    if tipo == "criar_conta":
        db.criar_conta(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            vencimento=dados["vencimento"],
            fornecedor=dados.get("fornecedor"),
            categoria=dados.get("categoria"),
        )
        return f"Conta registrada: {dados['descricao']} — {_formatar_valor(dados['valor'])} — vence {dados['vencimento']}"

    if tipo == "criar_gasto":
        db.criar_gasto(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            data_gasto=dados.get("data", date.today().isoformat()),
            categoria=dados.get("categoria"),
        )
        return f"Gasto registrado: {dados['descricao']} — {_formatar_valor(dados['valor'])}"

    if tipo == "criar_receita":
        db.criar_receita(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            data_receita=dados.get("data", date.today().isoformat()),
            categoria=dados.get("categoria"),
        )
        return f"Receita registrada: {dados['descricao']} — {_formatar_valor(dados['valor'])}"

    if tipo == "criar_aluguel":
        db.criar_aluguel(
            telefone=telefone,
            imovel=dados["imovel"],
            valor=float(dados["valor"]),
            vencimento=dados["vencimento"],
            locatario=dados.get("locatario"),
        )
        return f"Aluguel registrado: {dados['imovel']} — {_formatar_valor(dados['valor'])} — vence {dados['vencimento']}"

    if tipo == "criar_fornecedor":
        db.criar_fornecedor(
            telefone=telefone,
            nome=dados["nome"],
            contato=dados.get("contato"),
            categoria=dados.get("categoria"),
        )
        return f"Fornecedor cadastrado: {dados['nome']}"

    if tipo == "apagar_gasto":
        db.apagar_gasto(telefone, int(dados["id"]))
        return f"Gasto apagado: {dados.get('descricao', '')}"

    if tipo == "apagar_conta":
        db.apagar_conta(telefone, int(dados["id"]))
        return f"Conta apagada: {dados.get('descricao', '')}"

    if tipo == "apagar_receita":
        db.apagar_receita(telefone, int(dados["id"]))
        return f"Receita apagada: {dados.get('descricao', '')}"

    if tipo == "apagar_fornecedor":
        db.apagar_fornecedor(telefone, int(dados["id"]))
        return f"Fornecedor apagado: {dados.get('nome', '')}"

    if tipo == "marcar_pago":
        db.marcar_conta_paga(telefone, int(dados["id"]))
        return f"Conta marcada como paga: {dados.get('descricao', '')}"

    if tipo == "resetar_conta":
        db.resetar_usuario(telefone)
        return "Conta resetada — todos os dados foram apagados"

    return "Ação executada."


# ============================================================
# Helpers de confirmação (preview padronizado)
# ============================================================

_ALERTA_VALOR_ALTO = 1500.0  # Confirmação especial acima deste valor

_BOTOES_CONFIRMACAO = [
    {"id": "confirmar_sim", "text": "Confirmar"},
    {"id": "confirmar_nao", "text": "Cancelar"},
]


def _criar_pending_e_resposta(
    telefone: str, mensagem: str,
    action_type: str, action_data: dict,
    preview: str, verbo: str = "registrar",
) -> AgentResponse:
    """Cria pending_action e retorna AgentResponse DIRETO (sem LLM).

    Isso garante que a confirmação SEMPRE aparece — o LLM não
    pode alucinar dizendo que já executou.
    """
    valor = action_data.get("valor")
    alerta = ""
    if valor is not None and float(valor) > _ALERTA_VALOR_ALTO:
        alerta = (
            f"\n\n⚠️ *Atenção: valor acima de {_formatar_valor(_ALERTA_VALOR_ALTO)}!*"
        )

    db.criar_pending_action(
        telefone=telefone, action_type=action_type,
        action_data=action_data, preview=preview,
    )

    texto = (
        f"{preview}{alerta}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"*Posso {verbo}?*\n\n"
        f"✅ *SIM* — confirmar\n"
        f"❌ *NÃO* — cancelar\n"
        f"━━━━━━━━━━━━━━━"
    )

    db.salvar_conversa(telefone, mensagem, texto)
    return AgentResponse(text=texto, buttons=_BOTOES_CONFIRMACAO)


# ============================================================
# Onboarding determinístico (sem LLM para o fluxo principal)
# ============================================================

_FAIXAS_SALARIAIS = [
    "Até R$ 3.000",
    "R$ 3.000 – R$ 7.000",
    "R$ 7.000 – R$ 15.000",
    "Acima de R$ 15.000",
]

_FAIXAS_FATURAMENTO = [
    "Até R$ 50 mil/mês",
    "R$ 50 mil – R$ 200 mil/mês",
    "R$ 200 mil – R$ 1 milhão/mês",
    "Acima de R$ 1 milhão/mês",
]

_ONBOARDING_WELCOME = (
    "Olá! Sou a *Maria*, sua assistente financeira inteligente da "
    "*Evolution Financeiro*. 💼\n\n"
    "Vou te ajudar a organizar suas finanças de forma simples e "
    "eficiente — tudo aqui pelo WhatsApp.\n\n"
    "Para começar, me diz: você vai usar a ferramenta para *uso pessoal* "
    "ou para sua *empresa*?"
)


def _detectar_tipo(msg: str) -> str | None:
    """Detecta se a mensagem indica pessoal ou empresarial."""
    m = msg.lower().strip()
    pessoal_kw = ("pessoal", "pessoa", "pra mim", "uso pessoal", "pessoa física", "pf")
    empresa_kw = ("empresa", "empresarial", "negócio", "negocio", "pj",
                  "pessoa jurídica", "pessoa juridica", "cnpj", "minha empresa")
    for kw in empresa_kw:
        if kw in m:
            return "empresarial"
    for kw in pessoal_kw:
        if kw in m:
            return "pessoal"
    # Resposta de botão exata
    if m in ("1", "uso pessoal"):
        return "pessoal"
    if m in ("2", "uso empresarial", "empresa"):
        return "empresarial"
    return None


def _detectar_faixa(msg: str, faixas: list[str]) -> str | None:
    """Detecta faixa salarial/faturamento por número ou texto."""
    m = msg.strip()
    if m.isdigit() and 1 <= int(m) <= len(faixas):
        return faixas[int(m) - 1]
    ml = m.lower()
    for f in faixas:
        if f.lower() in ml or ml in f.lower():
            return f
    return None


def _extrair_nome(msg: str) -> str | None:
    """Extrai nome de uma mensagem curta (heurística simples)."""
    m = msg.strip()
    # Remove prefixos comuns
    for prefix in ("meu nome é", "me chamo", "pode me chamar de", "sou o", "sou a",
                   "é", "nome:", "me chame de"):
        if m.lower().startswith(prefix):
            m = m[len(prefix):].strip()
    # Nome deve ter 2-60 chars, sem números
    if 2 <= len(m) <= 60 and not any(c.isdigit() for c in m):
        return m.title()
    return None


def _processar_onboarding(
    telefone: str, mensagem: str,
    usuario: dict, historico: list[dict], dados: dict
) -> AgentResponse:
    """Onboarding determinístico — fluxo sem LLM, profissional e rápido."""

    # === ETAPA 1: Primeiro contato — mostrar boas-vindas + botões tipo ===
    if not historico and not usuario.get("tipo"):
        db.salvar_conversa(telefone, mensagem, _ONBOARDING_WELCOME)
        return AgentResponse(
            text=_ONBOARDING_WELCOME,
            buttons=[
                {"id": "tipo_pessoal", "text": "Uso Pessoal"},
                {"id": "tipo_empresarial", "text": "Uso Empresarial"},
            ],
        )

    # === ETAPA 2: Detectar tipo (pessoal/empresarial) ===
    if not usuario.get("tipo"):
        tipo = _detectar_tipo(mensagem)
        if not tipo:
            # Tentar via dados do classificador
            tipo = dados.get("tipo")
        if tipo in ("pessoal", "empresarial"):
            db.atualizar_usuario(telefone, tipo=tipo)
            usuario["tipo"] = tipo
        else:
            resp = (
                "Desculpe, não entendi. Por favor, escolha uma opção:\n\n"
                "1️⃣ *Uso Pessoal*\n"
                "2️⃣ *Uso Empresarial*"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(
                text=resp,
                buttons=[
                    {"id": "tipo_pessoal", "text": "Uso Pessoal"},
                    {"id": "tipo_empresarial", "text": "Uso Empresarial"},
                ],
            )

    # === FLUXO PESSOAL ===
    if usuario.get("tipo") == "pessoal":
        # Pedir nome se não tem
        if not usuario.get("nome"):
            nome = _extrair_nome(mensagem) or dados.get("nome")
            if nome:
                db.atualizar_usuario(telefone, nome=nome)
                usuario["nome"] = nome
            else:
                resp = (
                    "Ótimo, *uso pessoal*! 👤\n\n"
                    "Para personalizar sua experiência, me diz:\n"
                    "Como posso te chamar?"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

        # Pedir faixa salarial se não tem
        if not usuario.get("faixa_salarial"):
            faixa = _detectar_faixa(mensagem, _FAIXAS_SALARIAIS)
            if faixa and usuario.get("nome"):
                # Nome já veio antes, agora temos a faixa
                db.atualizar_usuario(telefone, faixa_salarial=faixa, onboarding_completo=True)
                usuario["faixa_salarial"] = faixa
                usuario["onboarding_completo"] = True
            elif usuario.get("nome") and not faixa:
                # Já tem nome, pede faixa
                nome = usuario["nome"]
                resp = (
                    f"Prazer, *{nome}*! 😊\n\n"
                    "Para te ajudar melhor, qual sua faixa de renda mensal?\n\n"
                    "1️⃣ Até R$ 3.000\n"
                    "2️⃣ R$ 3.000 – R$ 7.000\n"
                    "3️⃣ R$ 7.000 – R$ 15.000\n"
                    "4️⃣ Acima de R$ 15.000"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(
                    text=resp,
                    buttons=[
                        {"id": "faixa_1", "text": "Até R$ 3.000"},
                        {"id": "faixa_2", "text": "R$ 3k – R$ 7k"},
                        {"id": "faixa_3", "text": "R$ 7k – R$ 15k"},
                    ],
                )
            else:
                # Não conseguiu extrair nada — pede nome novamente
                resp = "Como posso te chamar? Me diz seu nome 😊"
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

        # Finalizar onboarding pessoal
        if not usuario.get("onboarding_completo"):
            db.atualizar_usuario(telefone, onboarding_completo=True)
            usuario["onboarding_completo"] = True

        nome = usuario.get("nome", "")
        resp = (
            f"Perfeito, *{nome}*! Seu cadastro está completo. ✅\n\n"
            "Agora posso te ajudar com:\n"
            "📝 Registrar contas a pagar\n"
            "💸 Controlar gastos e despesas\n"
            "💰 Registrar receitas\n"
            "📊 Gerar resumos e gráficos\n"
            "⏰ Alertas de vencimento diários\n\n"
            "Como posso te ajudar hoje?"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp)

    # === FLUXO EMPRESARIAL ===
    if usuario.get("tipo") == "empresarial":
        # Pedir CNPJ se não tem
        if not usuario.get("cnpj"):
            cnpj_raw = dados.get("cnpj") or re.sub(r'\D', '', mensagem)
            if len(cnpj_raw) == 14:
                resultado_cnpj = consultar_cnpj(cnpj_raw)
                if resultado_cnpj:
                    nome_empresa = resultado_cnpj["fantasia"] or resultado_cnpj["razao_social"]
                    db.atualizar_usuario(
                        telefone,
                        cnpj=resultado_cnpj["cnpj"],
                        razao_social=resultado_cnpj["razao_social"],
                        nome=nome_empresa,
                    )
                    usuario["cnpj"] = resultado_cnpj["cnpj"]
                    usuario["razao_social"] = resultado_cnpj["razao_social"]
                    usuario["nome"] = nome_empresa

                    resp = (
                        f"Encontrei sua empresa na Receita Federal! ✅\n\n"
                        f"• *Razão Social:* {resultado_cnpj['razao_social']}\n"
                        f"• *Nome Fantasia:* {resultado_cnpj['fantasia'] or '—'}\n"
                        f"• *Situação:* {resultado_cnpj['situacao']}\n"
                        f"• *Atividade:* {resultado_cnpj['atividade']}\n\n"
                        "Qual a faixa de faturamento mensal da empresa?\n\n"
                        "1️⃣ Até R$ 50 mil/mês\n"
                        "2️⃣ R$ 50 mil – R$ 200 mil/mês\n"
                        "3️⃣ R$ 200 mil – R$ 1 milhão/mês\n"
                        "4️⃣ Acima de R$ 1 milhão/mês"
                    )
                    db.salvar_conversa(telefone, mensagem, resp)
                    return AgentResponse(
                        text=resp,
                        buttons=[
                            {"id": "fat_1", "text": "Até R$ 50 mil"},
                            {"id": "fat_2", "text": "R$ 50k – R$ 200k"},
                            {"id": "fat_3", "text": "R$ 200k – R$ 1M"},
                        ],
                    )
                else:
                    resp = (
                        "Não consegui localizar esse CNPJ na Receita Federal. 😕\n"
                        "Verifique o número e envie novamente (somente os 14 dígitos)."
                    )
                    db.salvar_conversa(telefone, mensagem, resp)
                    return AgentResponse(text=resp)
            else:
                resp = (
                    "Ótimo, *uso empresarial*! 🏢\n\n"
                    "Para configurar sua conta, preciso do CNPJ da empresa.\n"
                    "Envie os *14 dígitos* do CNPJ:"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

        # Pedir faturamento se não tem
        if not usuario.get("faturamento"):
            faixa = _detectar_faixa(mensagem, _FAIXAS_FATURAMENTO)
            if faixa:
                db.atualizar_usuario(telefone, faturamento=faixa, onboarding_completo=True)
                usuario["faturamento"] = faixa
                usuario["onboarding_completo"] = True
            else:
                resp = (
                    "Qual a faixa de faturamento mensal da empresa?\n\n"
                    "1️⃣ Até R$ 50 mil/mês\n"
                    "2️⃣ R$ 50 mil – R$ 200 mil/mês\n"
                    "3️⃣ R$ 200 mil – R$ 1 milhão/mês\n"
                    "4️⃣ Acima de R$ 1 milhão/mês"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(
                    text=resp,
                    buttons=[
                        {"id": "fat_1", "text": "Até R$ 50 mil"},
                        {"id": "fat_2", "text": "R$ 50k – R$ 200k"},
                        {"id": "fat_3", "text": "R$ 200k – R$ 1M"},
                    ],
                )

        # Finalizar onboarding empresarial
        if not usuario.get("onboarding_completo"):
            db.atualizar_usuario(telefone, onboarding_completo=True)
            usuario["onboarding_completo"] = True

        nome = usuario.get("nome") or usuario.get("razao_social", "")
        resp = (
            f"Perfeito! Cadastro da *{nome}* concluído. ✅\n\n"
            "Agora posso te ajudar com:\n"
            "📝 Contas a pagar e boletos\n"
            "💸 Controle de despesas\n"
            "💰 Registro de receitas\n"
            "🤝 Cadastro de fornecedores\n"
            "📊 Resumos financeiros e gráficos\n"
            "⏰ Alertas de vencimento diários\n\n"
            "Como posso te ajudar hoje?"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp)

    # Fallback — não deveria chegar aqui
    resp = (
        "Para começar, me diz: você vai usar para *uso pessoal* ou para sua *empresa*?"
    )
    db.salvar_conversa(telefone, mensagem, resp)
    return AgentResponse(
        text=resp,
        buttons=[
            {"id": "tipo_pessoal", "text": "Uso Pessoal"},
            {"id": "tipo_empresarial", "text": "Uso Empresarial"},
        ],
    )


# ============================================================
# Pipeline principal
# ============================================================

_PALAVRAS_SIM = {"sim", "s", "confirmar", "confirma", "confirmo", "pode", "isso", "confirmar_sim", "yes", "ok"}
_PALAVRAS_NAO = {"não", "nao", "n", "cancelar", "cancela", "cancelo", "confirmar_nao", "no"}


def processar_mensagem(telefone: str, mensagem: str) -> AgentResponse:
    """Processa mensagem de texto e retorna AgentResponse (texto e/ou imagem)."""
    usuario = db.obter_ou_criar_usuario(telefone)
    historico = db.ultimas_conversas(telefone, limit=8)
    pending = db.obter_pending_action(telefone)

    # ── Fast-path: se há pending_action e resposta é sim/não, pula o LLM ──
    msg_lower = mensagem.strip().lower()
    if pending and msg_lower in _PALAVRAS_SIM:
        intencao = "confirmar"
        dados = {}
        logger.info("[%s] fast-path confirmar (pending=%s)", telefone, pending.get("action_type"))
    elif pending and msg_lower in _PALAVRAS_NAO:
        intencao = "cancelar"
        dados = {}
        logger.info("[%s] fast-path cancelar (pending=%s)", telefone, pending.get("action_type"))
    else:
        classificacao = classificar_intencao(mensagem, historico, pending, usuario)
        intencao = classificacao.get("intencao", "outro")
        dados = classificacao.get("dados", {}) or {}
    logger.info("[%s] intenção=%s dados=%s", telefone, intencao, dados)

    contexto = ""

    # ── Confirmação / cancelamento (determinístico — sem LLM) ──────────────
    if intencao == "confirmar":
        if pending:
            try:
                resultado = executar_pending_action(telefone, pending)
                db.limpar_pending_actions(telefone)
                logger.info("[%s] ação executada: %s", telefone, resultado)
                nome = usuario.get("nome") or ""
                resp = f"✅ Pronto{', ' + nome if nome else ''}! {resultado}\n\nComo mais posso te ajudar?"
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)
            except Exception as e:
                logger.error("[%s] erro ao executar pending_action: %s", telefone, e, exc_info=True)
                db.limpar_pending_actions(telefone)
                resp = "❌ Ops, ocorreu um erro ao processar. Pode tentar de novo?"
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)
        else:
            contexto = "O usuário confirmou mas não há ação pendente. Pergunte o que ele quer fazer."

    elif intencao == "cancelar":
        if pending:
            db.limpar_pending_actions(telefone)
            resp = "❌ Cancelado! Nada foi alterado.\n\nComo posso te ajudar?"
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        else:
            contexto = "Não havia nada pendente para cancelar. Pergunte o que o usuário quer fazer."

    # ── Onboarding (cadastro obrigatório) ─────────────────────────────────────
    elif intencao == "onboarding" or (not usuario.get("onboarding_completo") and intencao not in ("confirmar", "cancelar")):
        return _processar_onboarding(telefone, mensagem, usuario, historico, dados)

    # ── Saudação (cliente já cadastrado) ────────────────────────────────────
    elif intencao == "saudacao":
        nome = usuario.get("nome", "")
        contexto = (
            f"O cliente {nome} está cumprimentando. Responda de forma calorosa e breve. "
            f"Use o nome dele. Pergunte como pode ajudar hoje. "
            f"Não repita a apresentação completa — ele já conhece a Maria."
        )

    # ── Configuração de perfil (pós-onboarding) ──────────────────────────────
    elif intencao == "configurar_perfil":
        atualizacoes_perfil: dict = {}
        if dados.get("nome"):
            atualizacoes_perfil["nome"] = dados["nome"]
        if dados.get("tipo") in ("pessoal", "empresarial"):
            atualizacoes_perfil["tipo"] = dados["tipo"]
        if dados.get("orcamento_mensal") is not None:
            atualizacoes_perfil["orcamento_mensal"] = float(dados["orcamento_mensal"])
        if dados.get("cnpj"):
            resultado_cnpj = consultar_cnpj(dados["cnpj"])
            if resultado_cnpj:
                atualizacoes_perfil["cnpj"] = resultado_cnpj["cnpj"]
                atualizacoes_perfil["razao_social"] = resultado_cnpj["razao_social"]

        if atualizacoes_perfil:
            db.atualizar_usuario(telefone, **atualizacoes_perfil)
            usuario = {**usuario, **atualizacoes_perfil}
            partes = []
            if "nome" in atualizacoes_perfil:
                partes.append(f"nome: {atualizacoes_perfil['nome']}")
            if "tipo" in atualizacoes_perfil:
                partes.append(f"tipo: {atualizacoes_perfil['tipo']}")
            if "orcamento_mensal" in atualizacoes_perfil:
                partes.append(f"orçamento: {_formatar_valor(atualizacoes_perfil['orcamento_mensal'])}/mês")
            if "razao_social" in atualizacoes_perfil:
                partes.append(f"empresa: {atualizacoes_perfil['razao_social']}")
            contexto = f"Perfil atualizado — {', '.join(partes)}. Confirme brevemente."
        else:
            contexto = (
                "O cliente quer atualizar o perfil mas não informou o quê. "
                "Pergunte o que deseja alterar: nome, tipo de uso, orçamento mensal ou CNPJ."
            )

    # ── Registros ────────────────────────────────────────────────────────────
    elif intencao == "registrar_conta":
        if dados.get("valor") and dados.get("descricao"):
            vencimento = dados.get("vencimento") or date.today().isoformat()
            preview = (
                f"📝 *CONTA A PAGAR*\n"
                f"• Descrição: {dados['descricao']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Vencimento: {vencimento}\n"
                + (f"• Fornecedor: {dados['fornecedor']}\n" if dados.get("fornecedor") else "")
                + (f"• Categoria: {dados['categoria']}\n" if dados.get("categoria") else "")
            )
            return _criar_pending_e_resposta(telefone, mensagem, "criar_conta", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "vencimento": vencimento, "fornecedor": dados.get("fornecedor"),
                "categoria": dados.get("categoria"),
            }, preview)
        else:
            faltam = []
            if not dados.get("descricao"):
                faltam.append("descrição (ex: Conta de luz)")
            if not dados.get("valor"):
                faltam.append("valor (ex: 150)")
            contexto = f"Faltam dados para registrar a conta. Peça TUDO de uma vez: {', '.join(faltam)}. Dê exemplos curtos."

    elif intencao == "registrar_gasto":
        if dados.get("valor") and dados.get("descricao"):
            data_gasto = dados.get("data") or date.today().isoformat()
            preview = (
                f"💸 *GASTO*\n"
                f"• Descrição: {dados['descricao']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Data: {data_gasto}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get("categoria") else "")
            )
            return _criar_pending_e_resposta(telefone, mensagem, "criar_gasto", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "data": data_gasto, "categoria": dados.get("categoria"),
            }, preview)
        else:
            faltam = []
            if not dados.get("descricao"):
                faltam.append("descrição")
            if not dados.get("valor"):
                faltam.append("valor")
            contexto = f"Faltam dados para registrar o gasto. Peça TUDO de uma vez: {', '.join(faltam)}. Exemplo: 'Almoço 35 reais'."

    elif intencao == "registrar_receita":
        if dados.get("valor") and dados.get("descricao"):
            data_receita = dados.get("data") or date.today().isoformat()
            preview = (
                f"💰 *RECEITA*\n"
                f"• Descrição: {dados['descricao']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Data: {data_receita}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get("categoria") else "")
            )
            return _criar_pending_e_resposta(telefone, mensagem, "criar_receita", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "data": data_receita, "categoria": dados.get("categoria"),
            }, preview)
        else:
            faltam = []
            if not dados.get("descricao"):
                faltam.append("descrição")
            if not dados.get("valor"):
                faltam.append("valor")
            contexto = f"Faltam dados para registrar a receita. Peça TUDO de uma vez: {', '.join(faltam)}. Exemplo: 'Venda de produto R$ 500'."

    elif intencao == "registrar_aluguel":
        if dados.get("valor") and dados.get("imovel"):
            vencimento = dados.get("vencimento") or date.today().isoformat()
            preview = (
                f"🏠 *ALUGUEL*\n"
                f"• Imóvel: {dados['imovel']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Vencimento: {vencimento}\n"
                + (f"• Locatário: {dados['locatario']}\n" if dados.get("locatario") else "")
            )
            return _criar_pending_e_resposta(telefone, mensagem, "criar_aluguel", {
                "imovel": dados["imovel"], "valor": dados["valor"],
                "vencimento": vencimento, "locatario": dados.get("locatario"),
            }, preview)
        else:
            faltam = []
            if not dados.get("imovel"):
                faltam.append("nome/endereço do imóvel")
            if not dados.get("valor"):
                faltam.append("valor do aluguel")
            contexto = f"Faltam dados para registrar o aluguel. Peça TUDO de uma vez: {', '.join(faltam)}."

    elif intencao == "cadastrar_fornecedor":
        nome = dados.get("fornecedor")
        if nome:
            preview = (
                f"🤝 *FORNECEDOR*\n"
                f"• Nome: {nome}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get("categoria") else "")
            )
            return _criar_pending_e_resposta(telefone, mensagem, "criar_fornecedor", {
                "nome": nome, "categoria": dados.get("categoria"),
            }, preview, verbo="cadastrar")
        else:
            contexto = "O usuário quer cadastrar um fornecedor mas não informou o nome. Pergunte nome e categoria de uma vez."

    # ── Exclusões ────────────────────────────────────────────────────────────
    elif intencao == "apagar_gasto":
        gastos = db.listar_gastos(telefone, "mes")
        if not gastos:
            contexto = "O cliente quer apagar um gasto, mas não há gastos registrados este mês."
        else:
            lista = "\n".join(
                f"  {i+1}. {g['descricao']} — {_formatar_valor(g['valor'])} ({g.get('data', '')})"
                for i, g in enumerate(gastos[:10])
            )
            # Tenta match por descrição
            desc = (dados.get("descricao") or "").lower()
            match = next((g for g in gastos if desc and desc in g.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR GASTO*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_gasto",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                contexto = (
                    f"Gastos recentes:\n{lista}\n\n"
                    "Pergunte qual desses o cliente quer apagar (pelo número ou descrição)."
                )

    elif intencao == "apagar_conta":
        contas = db.listar_contas(telefone, status="pendente")
        if not contas:
            contexto = "O cliente quer apagar uma conta, mas não há contas pendentes."
        else:
            lista = "\n".join(
                f"  {i+1}. {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                for i, c in enumerate(contas[:10])
            )
            desc = (dados.get("descricao") or "").lower()
            match = next((c for c in contas if desc and desc in c.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR CONTA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_conta",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                contexto = (
                    f"Contas pendentes:\n{lista}\n\n"
                    "Pergunte qual o cliente quer apagar."
                )

    elif intencao == "apagar_receita":
        receitas = db.listar_receitas(telefone, "mes")
        if not receitas:
            contexto = "O cliente quer apagar uma receita, mas não há receitas registradas este mês."
        else:
            lista = "\n".join(
                f"  {i+1}. {r['descricao']} — {_formatar_valor(r['valor'])}"
                for i, r in enumerate(receitas[:10])
            )
            desc = (dados.get("descricao") or "").lower()
            match = next((r for r in receitas if desc and desc in r.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR RECEITA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_receita",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                contexto = (
                    f"Receitas recentes:\n{lista}\n\n"
                    "Pergunte qual o cliente quer apagar."
                )

    elif intencao == "apagar_fornecedor":
        fornecedores = db.listar_fornecedores(telefone)
        if not fornecedores:
            contexto = "O cliente quer apagar um fornecedor, mas não há fornecedores cadastrados."
        else:
            lista = "\n".join(
                f"  {i+1}. {f['nome']}"
                for i, f in enumerate(fornecedores[:10])
            )
            nome = (dados.get("fornecedor") or dados.get("descricao") or "").lower()
            match = next((f for f in fornecedores if nome and nome in f.get("nome", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR FORNECEDOR*\n• {match['nome']}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_fornecedor",
                    {"id": match["id"], "nome": match["nome"]},
                    preview, verbo="apagar")
            else:
                contexto = (
                    f"Fornecedores:\n{lista}\n\n"
                    "Pergunte qual o cliente quer apagar."
                )

    elif intencao == "marcar_pago":
        contas = db.listar_contas(telefone, status="pendente")
        if not contas:
            contexto = "Não há contas pendentes para marcar como paga."
        else:
            lista = "\n".join(
                f"  {i+1}. {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                for i, c in enumerate(contas[:10])
            )
            desc = (dados.get("descricao") or "").lower()
            match = next((c for c in contas if desc and desc in c.get("descricao", "").lower()), None)
            if match:
                preview = f"✅ *MARCAR COMO PAGA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "marcar_pago",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="marcar como paga")
            else:
                contexto = (
                    f"Contas pendentes:\n{lista}\n\n"
                    "Pergunte qual conta o cliente já pagou."
                )

    elif intencao == "resetar_conta":
        preview = (
            "⚠️ *ATENÇÃO — RESETAR CONTA*\n\n"
            "Isso vai apagar TODOS os seus dados:\n"
            "• Contas, gastos, receitas\n"
            "• Fornecedores, aluguéis\n"
            "• Histórico de conversas\n"
            "• Seu perfil será zerado\n\n"
            "*Essa ação é IRREVERSÍVEL!*"
        )
        return _criar_pending_e_resposta(telefone, mensagem, "resetar_conta", {}, preview, verbo="resetar")

    # ── Consultas ────────────────────────────────────────────────────────────
    elif intencao == "consultar_contas":
        contas = db.contas_proximas_vencimento(telefone, 30)
        if contas:
            lista = "\n".join(
                f"• {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                for c in contas
            )
            contexto = f"Contas pendentes próximas do vencimento (30 dias):\n{lista}"
        else:
            contexto = "Não há contas pendentes próximas do vencimento."

    elif intencao == "consultar_gastos":
        periodo = dados.get("periodo") or "mes"
        total = db.total_gastos(telefone, periodo)
        gastos = db.listar_gastos(telefone, periodo)
        if gastos:
            lista = "\n".join(
                f"• {g['descricao']} — {_formatar_valor(g['valor'])} ({g.get('categoria', 'sem categoria')})"
                for g in gastos[:10]
            )
            contexto = f"Gastos ({periodo}) — Total: {_formatar_valor(total)}\n{lista}"
        else:
            contexto = f"Nenhum gasto registrado no período ({periodo})."

    elif intencao == "consultar_receitas":
        periodo = dados.get("periodo") or "mes"
        total = db.total_receitas(telefone, periodo)
        receitas = db.listar_receitas(telefone, periodo)
        if receitas:
            lista = "\n".join(
                f"• {r['descricao']} — {_formatar_valor(r['valor'])} ({r.get('categoria', 'sem categoria')})"
                for r in receitas[:10]
            )
            contexto = f"Receitas ({periodo}) — Total: {_formatar_valor(total)}\n{lista}"
        else:
            contexto = f"Nenhuma receita registrada no período ({periodo})."

    elif intencao == "fluxo_caixa":
        periodo = dados.get("periodo") or "mes"
        fc = db.fluxo_caixa(telefone, periodo)
        sinal = "positivo" if fc["saldo"] >= 0 else "negativo"
        contexto = (
            f"Fluxo de caixa ({periodo}):\n"
            f"• Receitas: {_formatar_valor(fc['receitas'])}\n"
            f"• Gastos: {_formatar_valor(fc['gastos'])}\n"
            f"• Saldo: {_formatar_valor(fc['saldo'])} ({sinal})"
        )

    elif intencao == "consultar_fornecedores":
        fornecedores = db.listar_fornecedores(telefone)
        if fornecedores:
            lista = "\n".join(f"• {f['nome']} ({f.get('categoria', 'sem categoria')})" for f in fornecedores)
            contexto = f"Fornecedores cadastrados:\n{lista}"
        else:
            contexto = "Nenhum fornecedor cadastrado ainda."

    elif intencao == "consultar_alugueis":
        alugueis = db.listar_alugueis(telefone)
        if alugueis:
            lista = "\n".join(
                f"• {a['imovel']} — {_formatar_valor(a['valor'])} — vence {a['vencimento']} ({a.get('status', '')})"
                for a in alugueis
            )
            contexto = f"Aluguéis:\n{lista}"
        else:
            contexto = "Nenhum aluguel registrado."

    elif intencao == "resumo_financeiro":
        periodo = dados.get("periodo") or "mes"
        resumo = db.resumo_financeiro(telefone, periodo)
        contexto = (
            f"Resumo financeiro ({periodo}):\n"
            f"• Total de gastos: {_formatar_valor(resumo['total_gastos'])} ({resumo['quantidade_gastos']} registros)\n"
            f"• Contas pendentes: {resumo['contas_pendentes']} ({_formatar_valor(resumo['total_contas_pendentes'])})\n"
            f"• Aluguéis pendentes: {resumo['alugueis_pendentes']} ({_formatar_valor(resumo['total_alugueis_pendentes'])})\n"
            f"• Gastos por categoria: {json.dumps(resumo['gastos_por_categoria'], ensure_ascii=False)}"
        )
        proximas = resumo.get("proximas_vencimento", [])
        if proximas:
            lista = "\n".join(
                f"  ⚠️ {c['descricao']} — {_formatar_valor(c['valor'])} — {c['vencimento']}"
                for c in proximas
            )
            contexto += f"\nContas vencendo nos próximos 7 dias:\n{lista}"
        if usuario.get("orcamento_mensal"):
            orc = float(usuario["orcamento_mensal"])
            pct = (resumo["total_gastos"] / orc * 100) if orc > 0 else 0
            contexto += f"\nOrçamento mensal: {_formatar_valor(orc)} — utilizado: {pct:.1f}%"
            if pct > 90:
                contexto += " ⚠️ ALERTA: acima de 90% do orçamento!"
            elif pct > 75:
                contexto += " — atenção, acima de 75%"

    elif intencao == "dica_financeira":
        try:
            resumo = db.resumo_financeiro(telefone, "mes")
            fc = db.fluxo_caixa(telefone, "mes")
            contexto = (
                f"Dados do usuário — gastos do mês: {_formatar_valor(resumo['total_gastos'])}, "
                f"receitas: {_formatar_valor(fc['receitas'])}, "
                f"saldo: {_formatar_valor(fc['saldo'])}, "
                f"contas pendentes: {resumo['contas_pendentes']}. "
                f"Dê uma dica financeira específica baseada nesses dados."
            )
        except Exception:
            contexto = "Dê uma dica financeira geral prática."

    # ── Gráficos ─────────────────────────────────────────────────────────────
    elif intencao == "grafico_fornecedores":
        from app.charts import grafico_contas_por_fornecedor
        contas = db.listar_contas(telefone, status="pendente")
        if not contas:
            contexto = "Não há contas a pagar pendentes para gerar o gráfico. Sugira registrar contas primeiro."
        else:
            try:
                img_b64, caption = grafico_contas_por_fornecedor(contas)
                db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
                return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
            except Exception as e:
                logger.error("[%s] erro ao gerar gráfico de fornecedores: %s", telefone, e, exc_info=True)
                contexto = "Erro ao gerar o gráfico. Peça desculpas e sugira tentar novamente."

    elif intencao == "grafico_categorias":
        from app.charts import grafico_pizza_categorias
        periodo = dados.get("periodo") or "mes"
        gastos = db.listar_gastos(telefone, periodo)
        if not gastos:
            contexto = f"Não há gastos registrados no período ({periodo}) para gerar o gráfico."
        else:
            try:
                img_b64, caption = grafico_pizza_categorias(gastos)
                db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
                return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
            except Exception as e:
                logger.error("[%s] erro ao gerar gráfico de categorias: %s", telefone, e, exc_info=True)
                contexto = "Erro ao gerar o gráfico. Peça desculpas e sugira tentar novamente."

    elif intencao == "grafico_receita_gastos":
        from app.charts import grafico_receita_vs_gastos
        periodo = dados.get("periodo") or "mes"
        receitas = db.listar_receitas(telefone, periodo)
        gastos = db.listar_gastos(telefone, periodo)
        if not receitas and not gastos:
            contexto = "Não há dados de receitas ou gastos para gerar o gráfico. Registre algumas transações primeiro."
        else:
            try:
                img_b64, caption = grafico_receita_vs_gastos(receitas, gastos, periodo)
                db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
                return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
            except Exception as e:
                logger.error("[%s] erro ao gerar gráfico de fluxo: %s", telefone, e, exc_info=True)
                contexto = "Erro ao gerar o gráfico. Peça desculpas e sugira tentar novamente."

    # ── Gera resposta de texto ────────────────────────────────────────────────
    resposta = gerar_resposta(mensagem, historico, contexto, usuario=usuario)

    try:
        db.salvar_conversa(telefone, mensagem, resposta)
    except Exception as e:
        logger.error("[%s] erro ao salvar conversa: %s", telefone, e, exc_info=True)

    return AgentResponse(text=resposta)
