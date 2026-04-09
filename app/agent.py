import json
import logging
import os
from datetime import date
from openai import OpenAI
from dotenv import load_dotenv
from app.prompts import SYSTEM_PROMPT, INTENT_PROMPT
from app import database as db
from models.schemas import AgentResponse

load_dotenv()

logger = logging.getLogger(__name__)
client = OpenAI()
MODEL = "gpt-4o-mini"


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
        linhas.append(f"Usuário: {msg}\nFinBot: {resp}")
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
                         pending: dict | None = None) -> dict:
    """Classifica a intenção do usuário usando GPT, com contexto de pending_action."""
    historico_txt = _formatar_historico(historico)

    # Anotar ação pendente no contexto para evitar falhas de classificação
    if pending:
        contexto_ativo = (
            f"Há uma ação pendente de confirmação — "
            f"tipo: {pending['action_type']}, dados: {json.dumps(pending['action_data'], ensure_ascii=False)}"
        )
    else:
        contexto_ativo = "Nenhuma ação pendente no momento."

    prompt = (
        INTENT_PROMPT
        .replace("{contexto_ativo}", contexto_ativo)
        .replace("{historico}", historico_txt)
        .replace("{mensagem}", mensagem)
    )

    response = client.chat.completions.create(
        model=MODEL,
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


def gerar_resposta(mensagem: str, historico: list[dict], contexto: str = "") -> str:
    """Gera resposta conversacional usando o histórico recente como memória."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

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
        model=MODEL,
        messages=messages,
        max_tokens=1500,
        temperature=0.3,
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

    return "Ação executada."


# ============================================================
# Helpers de confirmação (preview padronizado)
# ============================================================

_CONFIRMACAO = (
    "\n\n━━━━━━━━━━━━━━━\n*Posso registrar?*\n\n"
    "✅ *SIM* — confirmar (ou reaja com 👍)\n"
    "❌ *NÃO* — cancelar (ou reaja com 👎)\n"
    "━━━━━━━━━━━━━━━"
)


def _criar_pending_e_contexto(telefone: str, action_type: str,
                               action_data: dict, preview: str,
                               verbo: str = "registrar") -> str:
    db.criar_pending_action(telefone=telefone, action_type=action_type,
                            action_data=action_data, preview=preview)
    return (
        f"NÃO foi salvo ainda — aguardando confirmação. "
        f"Mostre EXATAMENTE este preview e pergunte se pode {verbo}:\n\n{preview}"
        f"\n\nTermine com a pergunta de confirmação no formato EXATO:{_CONFIRMACAO}"
    )


# ============================================================
# Pipeline principal
# ============================================================

def processar_mensagem(telefone: str, mensagem: str) -> AgentResponse:
    """Processa mensagem de texto e retorna AgentResponse (texto e/ou imagem)."""
    historico = db.ultimas_conversas(telefone, limit=15)
    pending = db.obter_pending_action(telefone)
    classificacao = classificar_intencao(mensagem, historico, pending)
    intencao = classificacao.get("intencao", "outro")
    dados = classificacao.get("dados", {}) or {}
    logger.info("[%s] intenção=%s dados=%s", telefone, intencao, dados)

    contexto = ""

    # ── Confirmação / cancelamento ───────────────────────────────────────────
    if intencao == "confirmar":
        if pending:
            try:
                resultado = executar_pending_action(telefone, pending)
                db.limpar_pending_actions(telefone)
                logger.info("[%s] ação executada: %s", telefone, resultado)
                contexto = f"Ação confirmada e executada: {resultado}. Agradeça brevemente e ofereça ajuda."
            except Exception as e:
                logger.error("[%s] erro ao executar pending_action: %s", telefone, e, exc_info=True)
                contexto = "Erro ao executar a ação. Peça desculpas e sugira tentar novamente."
        else:
            contexto = "O usuário confirmou mas não há ação pendente. Pergunte o que ele quer fazer."

    elif intencao == "cancelar":
        if pending:
            db.limpar_pending_actions(telefone)
            contexto = "Ação cancelada. Confirme o cancelamento de forma simpática e pergunte como pode ajudar."
        else:
            contexto = "Não havia nada pendente para cancelar. Pergunte o que o usuário quer fazer."

    # ── Saudação ─────────────────────────────────────────────────────────────
    elif intencao == "saudacao":
        contexto = (
            "O usuário está cumprimentando. Responda de forma breve e amigável. "
            "Apresente em até 3 linhas as principais funcionalidades: registrar contas, "
            "gastos e receitas; consultar resumos e fluxo de caixa; gerar gráficos financeiros."
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
            contexto = _criar_pending_e_contexto(telefone, "criar_conta", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "vencimento": vencimento, "fornecedor": dados.get("fornecedor"),
                "categoria": dados.get("categoria"),
            }, preview)
        else:
            contexto = "Faltam dados para registrar a conta (valor e/ou descrição). Peça o que estiver faltando, um dado por vez."

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
            contexto = _criar_pending_e_contexto(telefone, "criar_gasto", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "data": data_gasto, "categoria": dados.get("categoria"),
            }, preview)
        else:
            contexto = "Faltam dados para registrar o gasto. Peça o que estiver faltando, um dado por vez."

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
            contexto = _criar_pending_e_contexto(telefone, "criar_receita", {
                "descricao": dados["descricao"], "valor": dados["valor"],
                "data": data_receita, "categoria": dados.get("categoria"),
            }, preview)
        else:
            contexto = "Faltam dados para registrar a receita. Peça o que estiver faltando, um dado por vez."

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
            contexto = _criar_pending_e_contexto(telefone, "criar_aluguel", {
                "imovel": dados["imovel"], "valor": dados["valor"],
                "vencimento": vencimento, "locatario": dados.get("locatario"),
            }, preview)
        else:
            contexto = "Faltam dados para registrar o aluguel. Peça o que estiver faltando, um dado por vez."

    elif intencao == "cadastrar_fornecedor":
        nome = dados.get("fornecedor")
        if nome:
            preview = (
                f"🤝 *FORNECEDOR*\n"
                f"• Nome: {nome}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get("categoria") else "")
            )
            contexto = _criar_pending_e_contexto(telefone, "criar_fornecedor", {
                "nome": nome, "categoria": dados.get("categoria"),
            }, preview, verbo="cadastrar")
        else:
            contexto = "O usuário quer cadastrar um fornecedor mas não informou o nome. Pergunte o nome."

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
    resposta = gerar_resposta(mensagem, historico, contexto)

    try:
        db.salvar_conversa(telefone, mensagem, resposta)
    except Exception as e:
        logger.error("[%s] erro ao salvar conversa: %s", telefone, e, exc_info=True)

    return AgentResponse(text=resposta)
