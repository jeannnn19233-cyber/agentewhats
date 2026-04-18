import json
import logging
import os
import re
from datetime import date, datetime
import httpx
from openai import OpenAI
from dotenv import load_dotenv
from app.prompts import SYSTEM_PROMPT, INTENT_PROMPT
from app import database as db
from models.schemas import AgentResponse

load_dotenv()

logger = logging.getLogger(__name__)


# ============================================================
# Parser de lote — entende listagens de contas coladas
# ============================================================

_MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

# Regex: DD/MM  valor  fornecedor  [✅]
_RE_LINHA_CONTA = re.compile(
    r'(\d{1,2})[/\-](\d{1,2})\s+'          # dia/mês
    r'([\d.,]+)\s+'                          # valor
    r'(.+?)$',                               # fornecedor (+ ✅)
    re.MULTILINE,
)

# Regex para cabeçalho de mês: *Abril*, **Maio**, Junho, etc.
_RE_MES_HEADER = re.compile(
    r'^\s*\*{0,2}([A-Za-zÀ-ú]+)\*{0,2}\s*$', re.MULTILINE
)


def _detectar_lote(mensagem: str) -> bool:
    """Detecta se a mensagem é uma listagem de contas em lote."""
    linhas_conta = _RE_LINHA_CONTA.findall(mensagem)
    return len(linhas_conta) >= 3  # 3+ linhas = é lote


def _parsear_lote_contas(mensagem: str) -> list[dict]:
    """Parseia listagem de contas colada.

    Formato esperado:
        *Abril*
        02/04 869,44 Fran Zelmar✅
        08/04 2.041,00 cosmetique
        ...

    Retorna lista de dicts:
        [{"data": "2026-04-02", "valor": 869.44,
          "fornecedor": "Fran Zelmar", "status": "pago"}, ...]
    """
    ano_atual = date.today().year
    contas = []

    # Tenta detectar mês do cabeçalho
    mes_atual = None
    headers = {}
    for m in _RE_MES_HEADER.finditer(mensagem):
        nome_mes = m.group(1).lower().strip()
        if nome_mes in _MESES:
            headers[m.start()] = _MESES[nome_mes]

    for m in _RE_LINHA_CONTA.finditer(mensagem):
        dia = int(m.group(1))
        mes_raw = int(m.group(2))
        valor_str = m.group(3).replace('.', '').replace(',', '.')
        resto = m.group(4).strip()

        try:
            valor = float(valor_str)
        except ValueError:
            continue

        # Detecta ✅ no final
        pago = '✅' in resto or '✓' in resto
        fornecedor = resto.replace('✅', '').replace('✓', '').strip()
        if not fornecedor:
            continue

        # Usa o mês da linha (DD/MM) como referência
        mes = mes_raw

        # Determina o ano (se mês > mês atual, pode ser ano que vem)
        ano = ano_atual

        try:
            data_venc = date(ano, mes, dia).isoformat()
        except ValueError:
            continue

        contas.append({
            "data": data_venc,
            "valor": valor,
            "fornecedor": fornecedor,
            "status": "pago" if pago else "pendente",
        })

    return contas
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

def executar_pending_action(telefone: str, action: dict,
                            criado_por: str | None = None) -> str:
    """Executa uma ação que estava aguardando confirmação."""
    tipo = action.get("action_type")
    dados = action.get("action_data") or {}
    _cb = criado_por or telefone  # quem criou o registro

    if tipo == "criar_conta":
        db.criar_conta(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            vencimento=dados["vencimento"],
            fornecedor=dados.get("fornecedor"),
            categoria=dados.get("categoria"),
            criado_por=_cb,
        )
        return f"Conta registrada: {dados['descricao']} — {_formatar_valor(dados['valor'])} — vence {dados['vencimento']}"

    if tipo == "criar_gasto":
        db.criar_gasto(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            data_gasto=dados.get("data", date.today().isoformat()),
            categoria=dados.get("categoria"),
            criado_por=_cb,
        )
        return f"Gasto registrado: {dados['descricao']} — {_formatar_valor(dados['valor'])}"

    if tipo == "criar_receita":
        db.criar_receita(
            telefone=telefone,
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            data_receita=dados.get("data", date.today().isoformat()),
            categoria=dados.get("categoria"),
            criado_por=_cb,
        )
        return f"Receita registrada: {dados['descricao']} — {_formatar_valor(dados['valor'])}"

    if tipo == "criar_aluguel":
        db.criar_aluguel(
            telefone=telefone,
            imovel=dados["imovel"],
            valor=float(dados["valor"]),
            vencimento=dados["vencimento"],
            locatario=dados.get("locatario"),
            criado_por=_cb,
        )
        return f"Aluguel registrado: {dados['imovel']} — {_formatar_valor(dados['valor'])} — vence {dados['vencimento']}"

    if tipo == "criar_fornecedor":
        db.criar_fornecedor(
            telefone=telefone,
            nome=dados["nome"],
            contato=dados.get("contato"),
            categoria=dados.get("categoria"),
            criado_por=_cb,
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

    if tipo == "adicionar_membro":
        db.adicionar_membro(
            telefone_novo=dados["telefone_membro"],
            empresa_id=dados["empresa_id"],
            nome=dados.get("nome"),
        )
        nome_m = dados.get("nome") or dados["telefone_membro"]
        return f"Membro adicionado: {nome_m}"

    if tipo == "remover_membro":
        db.remover_membro(
            telefone_membro=dados["telefone_membro"],
            empresa_id=dados["empresa_id"],
        )
        return f"Membro removido: {dados['telefone_membro']}"

    if tipo == "resetar_conta":
        db.resetar_usuario(telefone)
        return "Conta resetada — todos os dados foram apagados"

    if tipo == "importar_lote":
        contas = dados.get("contas", [])
        qtd = db.criar_contas_lote(telefone, contas, criado_por=_cb)
        pagas = sum(1 for c in contas if c.get("status") == "pago")
        pendentes = qtd - pagas
        return f"{qtd} contas importadas ({pagas} pagas, {pendentes} pendentes)"

    return "Ação executada."


# ============================================================
# Helpers de confirmação (preview padronizado)
# ============================================================

_ALERTA_VALOR_ALTO = 1500.0  # Confirmação especial acima deste valor

_BOTOES_CONFIRMACAO = [
    {"id": "confirmar_sim", "text": "Confirmar"},
    {"id": "confirmar_nao", "text": "Cancelar"},
]

# Menu principal — exibido após conclusão de ações
_MENU_BASE = (
    "\n\n📌 *O que deseja fazer agora?*\n\n"
    "1️⃣ Registrar conta\n"
    "2️⃣ Registrar gasto\n"
    "3️⃣ Registrar receita\n"
    "4️⃣ Consultar contas\n"
    "5️⃣ Resumo financeiro\n"
    "6️⃣ Ver gráficos\n"
)

_MENU_EQUIPE = (
    "7️⃣ Adicionar membro\n"
    "8️⃣ Minha equipe\n"
)

_MENU_RODAPE = "\nOu me diga o que precisa — estou aqui! 😊"


def _menu_texto(usuario: dict | None = None) -> str:
    """Retorna menu formatado — com opções de equipe para admins empresariais."""
    texto = _MENU_BASE
    if usuario and usuario.get("papel") == "admin" and usuario.get("empresa_id"):
        texto += _MENU_EQUIPE
    texto += _MENU_RODAPE
    return texto


def _menu_botoes(usuario: dict | None = None) -> list[dict]:
    """Retorna botões do menu — com equipe para admins."""
    botoes = [
        {"id": "menu_conta", "text": "📝 Registrar conta"},
        {"id": "menu_gasto", "text": "💸 Registrar gasto"},
        {"id": "menu_receita", "text": "💰 Registrar receita"},
    ]
    if usuario and usuario.get("papel") == "admin" and usuario.get("empresa_id"):
        botoes.append({"id": "menu_equipe", "text": "👥 Minha equipe"})
    return botoes


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

    # === MEMBRO CONVIDADO — fluxo simplificado ===
    if usuario.get("papel") == "membro" and usuario.get("empresa_id"):
        if not usuario.get("nome"):
            nome = _extrair_nome(mensagem) or dados.get("nome")
            if nome:
                db.atualizar_usuario(telefone, nome=nome, onboarding_completo=True)
                # Busca nome da empresa
                admin_info = db.obter_admin_empresa(usuario["empresa_id"])
                empresa_nome = admin_info.get("razao_social") or admin_info.get("nome", "sua empresa") if admin_info else "sua empresa"
                resp = (
                    f"Bem-vindo(a), *{nome}*! 🎉\n\n"
                    f"Você foi adicionado(a) como membro da *{empresa_nome}*.\n\n"
                    "Todos os dados são compartilhados com a empresa."
                    f"{_menu_texto(usuario)}"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
            else:
                resp = (
                    "Olá! Sou a *Maria*, assistente financeira da "
                    "*Evolution Financeiro*. 💼\n\n"
                    "Você foi convidado(a) para acessar os dados financeiros "
                    "da empresa. Para começar, me diz seu nome:"
                )
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)
        else:
            # Tem nome mas onboarding não concluído
            db.atualizar_usuario(telefone, onboarding_completo=True)
            nome = usuario["nome"]
            resp = (
                f"Pronto, *{nome}*! Seu acesso está ativo. ✅"
                f"{_menu_texto(usuario)}"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

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
            f"Perfeito, *{nome}*! Seu cadastro está completo. ✅"
            f"{_menu_texto(usuario)}"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

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
            f"Perfeito! Cadastro da *{nome}* concluído. ✅"
            f"{_menu_texto(usuario)}"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

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
_SAUDACOES = {"oi", "olá", "ola", "hey", "eai", "e aí", "bom dia", "boa tarde", "boa noite",
              "hello", "hi", "fala", "salve", "opa"}
_PEDIR_MENU = {"menu", "opções", "opcoes", "ajuda", "help", "o que você faz",
               "o que voce faz", "comandos", "início", "inicio", "voltar"}

# Mapeamento de botões de menu / respostas numéricas → intenção
_MENU_MAP: dict[str, str] = {
    "menu_conta": "registrar_conta",
    "menu_gasto": "registrar_gasto",
    "menu_receita": "registrar_receita",
    "menu_equipe": "listar_membros",
    "registrar conta": "registrar_conta",
    "registrar gasto": "registrar_gasto",
    "registrar receita": "registrar_receita",
    "minha equipe": "listar_membros",
    "👥 minha equipe": "listar_membros",
    "📝 registrar conta": "registrar_conta",
    "💸 registrar gasto": "registrar_gasto",
    "💰 registrar receita": "registrar_receita",
}

# Respostas numéricas do menu (após ação concluída)
_MENU_NUMERICO: dict[str, str] = {
    "1": "registrar_conta",
    "2": "registrar_gasto",
    "3": "registrar_receita",
    "4": "consultar_contas",
    "5": "resumo_financeiro",
    "6": "grafico_categorias",
    "7": "adicionar_membro",
    "8": "listar_membros",
}


def _processar_lote(telefone: str, mensagem: str, usuario: dict,
                    criado_por: str | None = None) -> AgentResponse:
    """Processa listagem de contas em lote — sem LLM, 100% parser."""
    contas = _parsear_lote_contas(mensagem)
    if not contas:
        resp = "Recebi sua lista mas não consegui interpretar as linhas. Use o formato:\n\nDD/MM valor fornecedor\nEx: 02/04 869,44 Fran Zelmar✅"
        db.salvar_conversa(telefone, mensagem[:200], resp)
        return AgentResponse(text=resp)

    pagas = [c for c in contas if c["status"] == "pago"]
    pendentes = [c for c in contas if c["status"] == "pendente"]
    total_geral = sum(c["valor"] for c in contas)
    total_pagas = sum(c["valor"] for c in pagas)
    total_pendentes = sum(c["valor"] for c in pendentes)

    # Amostra das primeiras 5 linhas
    amostra = contas[:5]
    amostra_txt = "\n".join(
        f"  • {c['data'][8:10]}/{c['data'][5:7]} — {_formatar_valor(c['valor'])} — {c['fornecedor']} {'✅' if c['status'] == 'pago' else '⏳'}"
        for c in amostra
    )
    if len(contas) > 5:
        amostra_txt += f"\n  ... e mais {len(contas) - 5} contas"

    preview = (
        f"📋 *IMPORTAÇÃO DE CONTAS EM LOTE*\n\n"
        f"Encontrei *{len(contas)} contas* na sua lista:\n\n"
        f"{amostra_txt}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Pagas: {len(pagas)} ({_formatar_valor(total_pagas)})\n"
        f"⏳ Pendentes: {len(pendentes)} ({_formatar_valor(total_pendentes)})\n"
        f"💰 Total: {_formatar_valor(total_geral)}\n"
        f"━━━━━━━━━━━━━━━"
    )

    return _criar_pending_e_resposta(
        telefone, mensagem[:200],
        "importar_lote", {"contas": contas},
        preview, verbo="importar tudo",
    )


def processar_mensagem(telefone: str, mensagem: str) -> AgentResponse:
    """Processa mensagem de texto e retorna AgentResponse (texto e/ou imagem)."""
    usuario = db.obter_ou_criar_usuario(telefone)

    # Resolve telefone de dados (compartilhado para membros de empresa)
    tel_dados = db._telefone_dados(usuario)

    # ── Pré-detecção: listagem em lote (pula LLM — economiza tokens) ──
    if _detectar_lote(mensagem):
        return _processar_lote(telefone, mensagem, usuario, criado_por=telefone)

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
    elif msg_lower in _SAUDACOES:
        intencao = "saudacao"
        dados = {}
        logger.info("[%s] fast-path saudação", telefone)
    elif msg_lower in _PEDIR_MENU:
        intencao = "saudacao"
        dados = {}
        logger.info("[%s] fast-path menu solicitado", telefone)
    elif msg_lower in _MENU_MAP:
        intencao = _MENU_MAP[msg_lower]
        dados = {}
        logger.info("[%s] fast-path menu: %s → %s", telefone, msg_lower, intencao)
    elif not pending and msg_lower in _MENU_NUMERICO:
        intencao = _MENU_NUMERICO[msg_lower]
        dados = {}
        logger.info("[%s] fast-path menu numérico: %s → %s", telefone, msg_lower, intencao)
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
                resultado = executar_pending_action(
                    tel_dados, pending, criado_por=telefone
                )
                db.limpar_pending_actions(telefone)
                logger.info("[%s] ação executada: %s", telefone, resultado)
                nome = usuario.get("nome") or ""
                resp = f"✅ Pronto{', ' + nome if nome else ''}! {resultado}{_menu_texto(usuario)}"
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
            except Exception as e:
                logger.error("[%s] erro ao executar pending_action: %s", telefone, e, exc_info=True)
                db.limpar_pending_actions(telefone)
                resp = f"❌ Ops, ocorreu um erro ao processar. Pode tentar de novo?{_menu_texto(usuario)}"
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = f"Não há nada pendente para confirmar.{_menu_texto(usuario)}"
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "cancelar":
        if pending:
            db.limpar_pending_actions(telefone)
            resp = f"❌ Cancelado! Nada foi alterado.{_menu_texto(usuario)}"
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = f"Não há nada pendente para cancelar.{_menu_texto(usuario)}"
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    # ── Onboarding (cadastro obrigatório) ─────────────────────────────────────
    elif intencao == "onboarding" or (not usuario.get("onboarding_completo") and intencao not in ("confirmar", "cancelar")):
        return _processar_onboarding(telefone, mensagem, usuario, historico, dados)

    # ── Saudação (cliente já cadastrado) ────────────────────────────────────
    elif intencao == "saudacao":
        nome = usuario.get("nome", "")
        saudacao = f"Oi, *{nome}*! 😊" if nome else "Oi! 😊"
        resp = f"{saudacao} Que bom te ver por aqui!{_menu_texto(usuario)}"
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

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
            resp = f"✅ Perfil atualizado: {', '.join(partes)}.{_menu_texto(usuario)}"
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = (
                "⚙️ *Atualizar perfil* — o que deseja alterar?\n\n"
                "1️⃣ Nome\n"
                "2️⃣ Tipo de uso (pessoal/empresarial)\n"
                "3️⃣ Orçamento mensal\n"
                "4️⃣ CNPJ\n\n"
                "Me diz o que quer mudar e o novo valor!"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

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
            resp = (
                "📝 Para registrar uma *conta a pagar*, preciso de:\n\n"
                "• *Descrição* — o que é a conta\n"
                "• *Valor* — quanto\n"
                "• *Vencimento* — quando vence\n\n"
                "Pode mandar tudo junto! Exemplo:\n"
                "_Conta de luz R$ 150 vence 20/05_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

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
            resp = (
                "💸 Para registrar um *gasto*, preciso de:\n\n"
                "• *Descrição* — no que gastou\n"
                "• *Valor* — quanto\n\n"
                "Pode mandar tudo junto! Exemplo:\n"
                "_Almoço R$ 35_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

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
            resp = (
                "💰 Para registrar uma *receita*, preciso de:\n\n"
                "• *Descrição* — de onde veio\n"
                "• *Valor* — quanto recebeu\n\n"
                "Pode mandar tudo junto! Exemplo:\n"
                "_Venda de produto R$ 500_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

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
            resp = (
                "🏠 Para registrar um *aluguel*, preciso de:\n\n"
                "• *Imóvel* — nome ou endereço\n"
                "• *Valor* — quanto\n"
                "• *Vencimento* — dia do vencimento\n\n"
                "Exemplo: _Sala comercial R$ 2.000 vence dia 10_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

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
            resp = (
                "🤝 Para cadastrar um *fornecedor*, preciso do:\n\n"
                "• *Nome* do fornecedor\n\n"
                "Exemplo: _Fornecedor Cosmetique_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

    # ── Exclusões ────────────────────────────────────────────────────────────
    elif intencao == "apagar_gasto":
        gastos = db.listar_gastos(tel_dados, "mes")
        if not gastos:
            resp = "Não há gastos registrados este mês para apagar." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            desc = (dados.get("descricao") or "").lower()
            match = next((g for g in gastos if desc and desc in g.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR GASTO*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_gasto",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                lista = "\n".join(
                    f"  {i+1}. {g['descricao']} — {_formatar_valor(g['valor'])} ({g.get('data', '')})"
                    for i, g in enumerate(gastos[:10])
                )
                resp = f"🗑️ Qual gasto deseja apagar?\n\n{lista}\n\nMe diz o número ou a descrição."
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

    elif intencao == "apagar_conta":
        contas = db.listar_contas(tel_dados, status="pendente")
        if not contas:
            resp = "Não há contas pendentes para apagar." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            desc = (dados.get("descricao") or "").lower()
            match = next((c for c in contas if desc and desc in c.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR CONTA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_conta",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                lista = "\n".join(
                    f"  {i+1}. {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                    for i, c in enumerate(contas[:10])
                )
                resp = f"🗑️ Qual conta deseja apagar?\n\n{lista}\n\nMe diz o número ou a descrição."
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

    elif intencao == "apagar_receita":
        receitas = db.listar_receitas(tel_dados, "mes")
        if not receitas:
            resp = "Não há receitas registradas este mês para apagar." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            desc = (dados.get("descricao") or "").lower()
            match = next((r for r in receitas if desc and desc in r.get("descricao", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR RECEITA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_receita",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="apagar")
            else:
                lista = "\n".join(
                    f"  {i+1}. {r['descricao']} — {_formatar_valor(r['valor'])}"
                    for i, r in enumerate(receitas[:10])
                )
                resp = f"🗑️ Qual receita deseja apagar?\n\n{lista}\n\nMe diz o número ou a descrição."
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

    elif intencao == "apagar_fornecedor":
        fornecedores = db.listar_fornecedores(tel_dados)
        if not fornecedores:
            resp = "Não há fornecedores cadastrados para apagar." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            nome = (dados.get("fornecedor") or dados.get("descricao") or "").lower()
            match = next((f for f in fornecedores if nome and nome in f.get("nome", "").lower()), None)
            if match:
                preview = f"🗑️ *APAGAR FORNECEDOR*\n• {match['nome']}"
                return _criar_pending_e_resposta(telefone, mensagem, "apagar_fornecedor",
                    {"id": match["id"], "nome": match["nome"]},
                    preview, verbo="apagar")
            else:
                lista = "\n".join(
                    f"  {i+1}. {f['nome']}"
                    for i, f in enumerate(fornecedores[:10])
                )
                resp = f"🗑️ Qual fornecedor deseja apagar?\n\n{lista}\n\nMe diz o número ou o nome."
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

    elif intencao == "marcar_pago":
        contas = db.listar_contas(tel_dados, status="pendente")
        if not contas:
            resp = "✅ Não há contas pendentes — tudo pago!" + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            desc = (dados.get("descricao") or "").lower()
            match = next((c for c in contas if desc and desc in c.get("descricao", "").lower()), None)
            if match:
                preview = f"✅ *MARCAR COMO PAGA*\n• {match['descricao']} — {_formatar_valor(match['valor'])}"
                return _criar_pending_e_resposta(telefone, mensagem, "marcar_pago",
                    {"id": match["id"], "descricao": match["descricao"], "valor": match["valor"]},
                    preview, verbo="marcar como paga")
            else:
                lista = "\n".join(
                    f"  {i+1}. {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                    for i, c in enumerate(contas[:10])
                )
                resp = f"✅ Qual conta você pagou?\n\n{lista}\n\nMe diz o número ou a descrição."
                db.salvar_conversa(telefone, mensagem, resp)
                return AgentResponse(text=resp)

    elif intencao == "adicionar_membro":
        if usuario.get("papel") != "admin":
            resp = "❌ Apenas o *administrador* da empresa pode adicionar membros."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        empresa_id = usuario.get("empresa_id")
        if not empresa_id:
            resp = "❌ Sua conta não está vinculada a uma empresa. Apenas contas empresariais podem ter membros."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        tel_novo = dados.get("telefone_membro") or ""
        tel_novo = re.sub(r'\D', '', tel_novo)
        nome_novo = dados.get("nome") or None
        if not tel_novo or len(tel_novo) < 10:
            resp = (
                "👥 Para adicionar um membro, preciso do:\n\n"
                "• *Telefone* do WhatsApp (com DDD)\n\n"
                "Exemplo: _Adicionar membro 82999999999_"
            )
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        else:
            preview = (
                f"👥 *ADICIONAR MEMBRO*\n"
                f"• Telefone: {tel_novo}\n"
                + (f"• Nome: {nome_novo}\n" if nome_novo else "")
                + f"• Empresa: {usuario.get('razao_social') or usuario.get('nome', '')}\n"
            )
            return _criar_pending_e_resposta(telefone, mensagem, "adicionar_membro", {
                "telefone_membro": tel_novo,
                "empresa_id": empresa_id,
                "nome": nome_novo,
            }, preview, verbo="adicionar")

    elif intencao == "remover_membro":
        if usuario.get("papel") != "admin":
            resp = "❌ Apenas o *administrador* da empresa pode remover membros."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        empresa_id = usuario.get("empresa_id")
        if not empresa_id:
            resp = "❌ Sua conta não está vinculada a uma empresa."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        membros = db.listar_membros_empresa(empresa_id)
        membros_nao_admin = [m for m in membros if m.get("papel") != "admin"]
        if not membros_nao_admin:
            resp = "Não há membros na empresa para remover (apenas você, admin)."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)
        tel_remover = dados.get("telefone_membro") or ""
        tel_remover = re.sub(r'\D', '', tel_remover)
        match_membro = next((m for m in membros_nao_admin if tel_remover and tel_remover in m.get("telefone", "")), None)
        if match_membro:
            nome_m = match_membro.get("nome") or match_membro["telefone"]
            preview = f"🗑️ *REMOVER MEMBRO*\n• {nome_m} ({match_membro['telefone']})"
            return _criar_pending_e_resposta(telefone, mensagem, "remover_membro", {
                "telefone_membro": match_membro["telefone"],
                "empresa_id": empresa_id,
            }, preview, verbo="remover")
        else:
            lista = "\n".join(
                f"  {i+1}. {m.get('nome') or '?'} — {m['telefone']}"
                for i, m in enumerate(membros_nao_admin)
            )
            resp = f"👥 Qual membro deseja remover?\n\n{lista}\n\nMe diz o número do telefone."
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp)

    elif intencao == "listar_membros":
        empresa_id = usuario.get("empresa_id")
        if not empresa_id:
            resp = "Sua conta não está vinculada a uma empresa." + _menu_texto(usuario)
        else:
            membros = db.listar_membros_empresa(empresa_id)
            if membros:
                linhas = [f"👥 *EQUIPE* ({len(membros)} membros)\n"]
                for m in membros:
                    papel_icon = "👑" if m.get("papel") == "admin" else "👤"
                    linhas.append(f"{papel_icon} {m.get('nome') or '?'} — {m['telefone']} ({m.get('papel', '?')})")
                resp = "\n".join(linhas) + _menu_texto(usuario)
            else:
                resp = "Nenhum membro cadastrado na empresa." + _menu_texto(usuario)
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

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

    # ── Consultas (determinísticas — sem LLM) ──────────────────────────────
    elif intencao == "consultar_contas":
        contas = db.listar_contas(tel_dados, status="pendente")
        if contas:
            total = sum(c["valor"] for c in contas)
            # Agrupa por mês
            meses: dict[str, list] = {}
            for c in contas:
                venc = c.get("vencimento", "")
                chave = venc[:7] if len(venc) >= 7 else "Sem data"  # AAAA-MM
                meses.setdefault(chave, []).append(c)

            partes = [f"📋 *CONTAS A PAGAR PENDENTES* ({len(contas)} contas — {_formatar_valor(total)})\n"]
            for mes_key in sorted(meses.keys()):
                # Formata cabeçalho do mês
                try:
                    m_num = int(mes_key[5:7])
                    m_ano = mes_key[:4]
                    nomes_mes = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                                 "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
                    cab = f"\n*{nomes_mes[m_num]}/{m_ano}*"
                except (ValueError, IndexError):
                    cab = f"\n*{mes_key}*"
                partes.append(cab)
                subtotal = 0.0
                for c in meses[mes_key]:
                    status_icon = "✅" if c.get("status") == "pago" else "⏳"
                    dia = c["vencimento"][8:10] if len(c.get("vencimento", "")) >= 10 else "?"
                    partes.append(
                        f"  {status_icon} {dia}/{mes_key[5:7]} — {_formatar_valor(c['valor'])} — {c['descricao']}"
                    )
                    subtotal += c["valor"]
                partes.append(f"  💰 Subtotal: {_formatar_valor(subtotal)}")

            resp = "\n".join(partes) + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = "✅ Nenhuma conta pendente no momento! Tudo em dia." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "consultar_gastos":
        periodo = dados.get("periodo") or "mes"
        total = db.total_gastos(tel_dados, periodo)
        gastos = db.listar_gastos(tel_dados, periodo)
        if gastos:
            linhas = [f"💸 *GASTOS ({periodo.upper()})* — Total: {_formatar_valor(total)}\n"]
            for g in gastos:
                data_g = g.get("data", "")
                dia = data_g[8:10] + "/" + data_g[5:7] if len(data_g) >= 10 else ""
                cat = g.get("categoria") or "sem categoria"
                linhas.append(f"• {dia} — {_formatar_valor(g['valor'])} — {g['descricao']} ({cat})")
            resp = "\n".join(linhas) + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = f"Nenhum gasto registrado no período ({periodo})." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "consultar_receitas":
        periodo = dados.get("periodo") or "mes"
        total = db.total_receitas(tel_dados, periodo)
        receitas = db.listar_receitas(tel_dados, periodo)
        if receitas:
            linhas = [f"💰 *RECEITAS ({periodo.upper()})* — Total: {_formatar_valor(total)}\n"]
            for r in receitas:
                data_r = r.get("data", "")
                dia = data_r[8:10] + "/" + data_r[5:7] if len(data_r) >= 10 else ""
                cat = r.get("categoria") or "sem categoria"
                linhas.append(f"• {dia} — {_formatar_valor(r['valor'])} — {r['descricao']} ({cat})")
            resp = "\n".join(linhas) + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        else:
            resp = f"Nenhuma receita registrada no período ({periodo})." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "fluxo_caixa":
        periodo = dados.get("periodo") or "mes"
        fc = db.fluxo_caixa(tel_dados, periodo)
        saldo_icon = "🟢" if fc["saldo"] >= 0 else "🔴"
        resp = (
            f"📊 *FLUXO DE CAIXA ({periodo.upper()})*\n\n"
            f"💰 Receitas: {_formatar_valor(fc['receitas'])}\n"
            f"💸 Gastos: {_formatar_valor(fc['gastos'])}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{saldo_icon} *Saldo: {_formatar_valor(fc['saldo'])}*"
            f"{_menu_texto(usuario)}"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "consultar_fornecedores":
        fornecedores = db.listar_fornecedores(tel_dados)
        if fornecedores:
            linhas = [f"🤝 *FORNECEDORES* ({len(fornecedores)})\n"]
            for f in fornecedores:
                cat = f.get("categoria") or "sem categoria"
                linhas.append(f"• {f['nome']} ({cat})")
            resp = "\n".join(linhas) + _menu_texto(usuario)
        else:
            resp = "Nenhum fornecedor cadastrado ainda." + _menu_texto(usuario)
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "consultar_alugueis":
        alugueis = db.listar_alugueis(tel_dados)
        if alugueis:
            linhas = [f"🏠 *ALUGUÉIS* ({len(alugueis)})\n"]
            for a in alugueis:
                status_icon = "✅" if a.get("status") == "pago" else "⏳"
                linhas.append(
                    f"{status_icon} {a['imovel']} — {_formatar_valor(a['valor'])} — vence {a['vencimento']}"
                )
            resp = "\n".join(linhas) + _menu_texto(usuario)
        else:
            resp = "Nenhum aluguel registrado." + _menu_texto(usuario)
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "resumo_financeiro":
        periodo = dados.get("periodo") or "mes"
        resumo = db.resumo_financeiro(tel_dados, periodo)
        fc = db.fluxo_caixa(tel_dados, periodo)
        saldo_icon = "🟢" if fc["saldo"] >= 0 else "🔴"

        linhas = [
            f"📊 *RESUMO FINANCEIRO ({periodo.upper()})*\n",
            f"💰 Receitas: {_formatar_valor(fc['receitas'])}",
            f"💸 Gastos: {_formatar_valor(resumo['total_gastos'])} ({resumo['quantidade_gastos']} registros)",
            f"📝 Contas pendentes: {resumo['contas_pendentes']} ({_formatar_valor(resumo['total_contas_pendentes'])})",
            f"🏠 Aluguéis pendentes: {resumo['alugueis_pendentes']} ({_formatar_valor(resumo['total_alugueis_pendentes'])})",
            f"━━━━━━━━━━━━━━━",
            f"{saldo_icon} *Saldo: {_formatar_valor(fc['saldo'])}*",
        ]

        # Gastos por categoria
        cats = resumo.get("gastos_por_categoria", {})
        if cats:
            linhas.append("\n*Gastos por categoria:*")
            for cat, val in sorted(cats.items(), key=lambda x: -x[1]):
                linhas.append(f"  • {cat}: {_formatar_valor(val)}")

        # Contas vencendo em breve
        proximas = resumo.get("proximas_vencimento", [])
        if proximas:
            linhas.append("\n⚠️ *Vencendo nos próximos 7 dias:*")
            for c in proximas:
                linhas.append(f"  • {c['descricao']} — {_formatar_valor(c['valor'])} — {c['vencimento']}")

        # Orçamento
        if usuario.get("orcamento_mensal"):
            orc = float(usuario["orcamento_mensal"])
            pct = (resumo["total_gastos"] / orc * 100) if orc > 0 else 0
            alerta = ""
            if pct > 90:
                alerta = " ⚠️ *ALERTA!*"
            elif pct > 75:
                alerta = " ⚡ Atenção"
            linhas.append(f"\n🎯 Orçamento: {_formatar_valor(orc)} — usado {pct:.0f}%{alerta}")

        resp = "\n".join(linhas) + _menu_texto(usuario)
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "dica_financeira":
        try:
            resumo = db.resumo_financeiro(tel_dados, "mes")
            fc = db.fluxo_caixa(tel_dados, "mes")
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
        contas = db.listar_contas(tel_dados, status="pendente")
        if not contas:
            resp = "📊 Não há contas pendentes para gerar o gráfico." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        try:
            img_b64, caption = grafico_contas_por_fornecedor(contas)
            db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
            return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
        except Exception as e:
            logger.error("[%s] erro ao gerar gráfico de fornecedores: %s", telefone, e, exc_info=True)
            resp = "❌ Erro ao gerar o gráfico. Tente novamente." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "grafico_categorias":
        from app.charts import grafico_pizza_categorias
        periodo = dados.get("periodo") or "mes"
        gastos = db.listar_gastos(tel_dados, periodo)
        if not gastos:
            resp = f"📊 Não há gastos no período ({periodo}) para gerar o gráfico." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        try:
            img_b64, caption = grafico_pizza_categorias(gastos)
            db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
            return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
        except Exception as e:
            logger.error("[%s] erro ao gerar gráfico de categorias: %s", telefone, e, exc_info=True)
            resp = "❌ Erro ao gerar o gráfico. Tente novamente." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    elif intencao == "grafico_receita_gastos":
        from app.charts import grafico_receita_vs_gastos
        periodo = dados.get("periodo") or "mes"
        receitas = db.listar_receitas(tel_dados, periodo)
        gastos = db.listar_gastos(tel_dados, periodo)
        if not receitas and not gastos:
            resp = "📊 Não há dados de receitas ou gastos para gerar o gráfico." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))
        try:
            img_b64, caption = grafico_receita_vs_gastos(receitas, gastos, periodo)
            db.salvar_conversa(telefone, mensagem, f"[GRÁFICO] {caption}")
            return AgentResponse(image_b64=img_b64, image_caption=caption, text="")
        except Exception as e:
            logger.error("[%s] erro ao gerar gráfico de fluxo: %s", telefone, e, exc_info=True)
            resp = "❌ Erro ao gerar o gráfico. Tente novamente." + _menu_texto(usuario)
            db.salvar_conversa(telefone, mensagem, resp)
            return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    # ── Intenção "outro" sem contexto → menu direto (sem LLM) ──────────────
    if intencao == "outro" and not contexto:
        nome = usuario.get("nome", "")
        saudacao = f"*{nome}*," if nome else ""
        resp = (
            f"Hmm, {saudacao} não entendi exatamente o que precisa. 🤔\n"
            f"Mas posso te ajudar com várias coisas!"
            f"{_menu_texto(usuario)}"
        )
        db.salvar_conversa(telefone, mensagem, resp)
        return AgentResponse(text=resp, buttons=_menu_botoes(usuario))

    # ── Gera resposta de texto (LLM — para contextos específicos) ──────────
    resposta = gerar_resposta(mensagem, historico, contexto, usuario=usuario)

    try:
        db.salvar_conversa(telefone, mensagem, resposta)
    except Exception as e:
        logger.error("[%s] erro ao salvar conversa: %s", telefone, e, exc_info=True)

    return AgentResponse(text=resposta)
