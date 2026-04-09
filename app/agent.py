import json
import os
from datetime import date
from openai import OpenAI
from dotenv import load_dotenv
from app.prompts import SYSTEM_PROMPT, INTENT_PROMPT
from app import database as db

load_dotenv()

client = OpenAI()
MODEL = "gpt-4o-mini"


# ============================================================
# Helpers
# ============================================================

def _formatar_historico(historico: list[dict]) -> str:
    """Formata o histórico de conversas como texto para incluir no prompt."""
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
# Classificação e geração de resposta (com memória)
# ============================================================

def classificar_intencao(mensagem: str, historico: list[dict]) -> dict:
    """Usa GPT para classificar a intenção do usuário com contexto da conversa."""
    historico_txt = _formatar_historico(historico)
    prompt = INTENT_PROMPT.replace("{historico}", historico_txt).replace("{mensagem}", mensagem)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0,
    )
    texto = response.choices[0].message.content or "{}"
    texto = texto.strip()
    if texto.startswith("```"):
        texto = texto.split("\n", 1)[-1]
    if texto.endswith("```"):
        texto = texto.rsplit("```", 1)[0]
    texto = texto.strip()

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return {"intencao": "outro", "dados": {}}


def gerar_resposta(mensagem: str, historico: list[dict], contexto: str = "") -> str:
    """Gera resposta conversacional usando o histórico recente como memória."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Injeta histórico como turnos reais de conversa (memória de curto prazo)
    for h in historico:
        msg = (h.get("mensagem") or "").strip()
        resp = (h.get("resposta") or "").strip()
        if msg:
            messages.append({"role": "user", "content": msg})
        if resp:
            messages.append({"role": "assistant", "content": resp})

    if contexto:
        messages.append({"role": "system", "content": f"Contexto adicional para esta resposta:\n{contexto}"})

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
# Pipeline principal
# ============================================================

def processar_mensagem(telefone: str, mensagem: str) -> str:
    """Processa uma mensagem de texto e retorna a resposta."""
    historico = db.ultimas_conversas(telefone, limit=15)
    classificacao = classificar_intencao(mensagem, historico)
    intencao = classificacao.get("intencao", "outro")
    dados = classificacao.get("dados", {}) or {}

    contexto = ""

    # ---------- Confirmação / cancelamento ----------
    if intencao == "confirmar":
        pendente = db.obter_pending_action(telefone)
        if pendente:
            try:
                resultado = executar_pending_action(telefone, pendente)
                db.limpar_pending_actions(telefone)
                contexto = f"Ação confirmada e executada com sucesso: {resultado}. Agradeça e ofereça ajuda adicional."
            except Exception as e:
                contexto = f"Houve um erro ao executar a ação. Peça desculpas e sugira tentar novamente."
        else:
            contexto = "O usuário disse 'sim' mas não há nenhuma ação pendente. Pergunte o que ele quer fazer."

    elif intencao == "cancelar":
        pendente = db.obter_pending_action(telefone)
        if pendente:
            db.limpar_pending_actions(telefone)
            contexto = "Ação cancelada conforme pedido do usuário. Confirme o cancelamento de forma simpática."
        else:
            contexto = "Não havia nada pendente para cancelar. Pergunte o que o usuário quer fazer."

    # ---------- Saudação ----------
    elif intencao == "saudacao":
        contexto = (
            "O usuário está cumprimentando. Responda de forma breve e amigável e apresente "
            "as principais funcionalidades disponíveis: registrar contas, gastos, aluguéis, "
            "consultar resumos financeiros e receber dicas. Seja conciso."
        )

    # ---------- Registros (criam pending action ao invés de salvar direto) ----------
    elif intencao == "registrar_conta":
        if dados.get("valor") and dados.get("descricao"):
            vencimento = dados.get("vencimento") or date.today().isoformat()
            preview = (
                f"📝 Vou registrar esta CONTA A PAGAR:\n"
                f"• Descrição: {dados['descricao']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Vencimento: {vencimento}\n"
                + (f"• Fornecedor: {dados['fornecedor']}\n" if dados.get('fornecedor') else "")
                + (f"• Categoria: {dados['categoria']}\n" if dados.get('categoria') else "")
            )
            db.criar_pending_action(
                telefone=telefone,
                action_type="criar_conta",
                action_data={
                    "descricao": dados["descricao"],
                    "valor": dados["valor"],
                    "vencimento": vencimento,
                    "fornecedor": dados.get("fornecedor"),
                    "categoria": dados.get("categoria"),
                },
                preview=preview,
            )
            contexto = (
                f"O usuário quer registrar uma conta. NÃO foi salva ainda — está aguardando confirmação. "
                f"Mostre EXATAMENTE este preview e pergunte se pode confirmar:\n\n{preview}\n\n"
                f"Termine com a pergunta de confirmação no formato EXATO:\n\n━━━━━━━━━━━━━━━\n*Posso registrar?*\n\n✅ *SIM* — confirmar (ou reaja com 👍)\n❌ *NÃO* — cancelar (ou reaja com 👎)\n━━━━━━━━━━━━━━━"
            )
        else:
            contexto = "O usuário quer registrar uma conta mas faltam dados (valor e/ou descrição). Peça os dados que faltam de forma direta."

    elif intencao == "registrar_gasto":
        if dados.get("valor") and dados.get("descricao"):
            data_gasto = dados.get("data") or date.today().isoformat()
            preview = (
                f"💸 Vou registrar este GASTO:\n"
                f"• Descrição: {dados['descricao']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Data: {data_gasto}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get('categoria') else "")
            )
            db.criar_pending_action(
                telefone=telefone,
                action_type="criar_gasto",
                action_data={
                    "descricao": dados["descricao"],
                    "valor": dados["valor"],
                    "data": data_gasto,
                    "categoria": dados.get("categoria"),
                },
                preview=preview,
            )
            contexto = (
                f"O usuário quer registrar um gasto. NÃO foi salvo ainda — está aguardando confirmação. "
                f"Mostre EXATAMENTE este preview e pergunte se pode confirmar:\n\n{preview}\n\n"
                f"Termine com a pergunta de confirmação no formato EXATO:\n\n━━━━━━━━━━━━━━━\n*Posso registrar?*\n\n✅ *SIM* — confirmar (ou reaja com 👍)\n❌ *NÃO* — cancelar (ou reaja com 👎)\n━━━━━━━━━━━━━━━"
            )
        else:
            contexto = "O usuário quer registrar um gasto mas faltam dados. Peça os dados que faltam de forma direta."

    elif intencao == "registrar_aluguel":
        if dados.get("valor") and dados.get("imovel"):
            vencimento = dados.get("vencimento") or date.today().isoformat()
            preview = (
                f"🏠 Vou registrar este ALUGUEL:\n"
                f"• Imóvel: {dados['imovel']}\n"
                f"• Valor: {_formatar_valor(dados['valor'])}\n"
                f"• Vencimento: {vencimento}\n"
                + (f"• Locatário: {dados['locatario']}\n" if dados.get('locatario') else "")
            )
            db.criar_pending_action(
                telefone=telefone,
                action_type="criar_aluguel",
                action_data={
                    "imovel": dados["imovel"],
                    "valor": dados["valor"],
                    "vencimento": vencimento,
                    "locatario": dados.get("locatario"),
                },
                preview=preview,
            )
            contexto = (
                f"O usuário quer registrar um aluguel. NÃO foi salvo ainda — está aguardando confirmação. "
                f"Mostre este preview e pergunte se pode confirmar:\n\n{preview}\n\n"
                f"Termine com a pergunta de confirmação no formato EXATO:\n\n━━━━━━━━━━━━━━━\n*Posso registrar?*\n\n✅ *SIM* — confirmar (ou reaja com 👍)\n❌ *NÃO* — cancelar (ou reaja com 👎)\n━━━━━━━━━━━━━━━"
            )
        else:
            contexto = "O usuário quer registrar um aluguel mas faltam dados (imóvel e/ou valor). Peça o que faltar."

    elif intencao == "cadastrar_fornecedor":
        nome_fornecedor = dados.get("fornecedor")
        if nome_fornecedor:
            preview = (
                f"🤝 Vou cadastrar este FORNECEDOR:\n"
                f"• Nome: {nome_fornecedor}\n"
                + (f"• Categoria: {dados['categoria']}\n" if dados.get('categoria') else "")
            )
            db.criar_pending_action(
                telefone=telefone,
                action_type="criar_fornecedor",
                action_data={
                    "nome": nome_fornecedor,
                    "categoria": dados.get("categoria"),
                },
                preview=preview,
            )
            contexto = (
                f"O usuário quer cadastrar um fornecedor. NÃO foi salvo ainda — está aguardando confirmação. "
                f"Mostre este preview e pergunte se pode confirmar:\n\n{preview}\n\n"
                f"Termine com a pergunta de confirmação no formato EXATO:\n\n━━━━━━━━━━━━━━━\n*Posso cadastrar?*\n\n✅ *SIM* — confirmar (ou reaja com 👍)\n❌ *NÃO* — cancelar (ou reaja com 👎)\n━━━━━━━━━━━━━━━"
            )
        else:
            contexto = "O usuário quer cadastrar um fornecedor mas não informou o nome. Pergunte o nome."

    # ---------- Consultas (não precisam de confirmação) ----------
    elif intencao == "consultar_contas":
        contas = db.contas_proximas_vencimento(telefone, 30)
        if contas:
            lista = "\n".join(
                f"• {c['descricao']} — {_formatar_valor(c['valor'])} — vence {c['vencimento']}"
                for c in contas
            )
            contexto = f"Contas pendentes próximas do vencimento:\n{lista}"
        else:
            contexto = "Não há contas pendentes próximas do vencimento."

    elif intencao == "consultar_gastos":
        periodo = dados.get("periodo", "mes")
        total = db.total_gastos(telefone, periodo)
        gastos = db.listar_gastos(telefone, periodo)
        if gastos:
            lista = "\n".join(
                f"• {g['descricao']} — {_formatar_valor(g['valor'])} ({g.get('categoria', 'sem categoria')})"
                for g in gastos[:10]
            )
            contexto = f"Total de gastos no período ({periodo}): {_formatar_valor(total)}\nÚltimos gastos:\n{lista}"
        else:
            contexto = f"Nenhum gasto registrado no período ({periodo})."

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
        periodo = dados.get("periodo", "mes")
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
            contexto = (
                f"Dados do usuário para basear a dica — gastos do mês: "
                f"{_formatar_valor(resumo['total_gastos'])}, contas pendentes: {resumo['contas_pendentes']}"
            )
        except Exception:
            contexto = "Dê uma dica financeira geral."

    # Gera a resposta conversacional usando a memória
    resposta = gerar_resposta(mensagem, historico, contexto)

    # Salva a conversa (vira histórico para a próxima)
    try:
        db.salvar_conversa(telefone, mensagem, resposta)
    except Exception:
        pass

    return resposta
