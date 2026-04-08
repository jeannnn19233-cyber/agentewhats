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


def classificar_intencao(mensagem: str) -> dict:
    """Usa GPT para classificar a intenção do usuário."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": INTENT_PROMPT.format(mensagem=mensagem)},
        ],
        max_tokens=300,
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


def gerar_resposta(mensagem: str, contexto: str = "") -> str:
    """Gera resposta conversacional com o agente financeiro."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    if contexto:
        messages.append({"role": "system", "content": f"Contexto adicional:\n{contexto}"})
    messages.append({"role": "user", "content": mensagem})

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=800,
        temperature=0.7,
    )
    return response.choices[0].message.content or "Desculpe, não consegui processar sua mensagem."


def processar_mensagem(telefone: str, mensagem: str) -> str:
    """Processa uma mensagem de texto e retorna a resposta."""
    classificacao = classificar_intencao(mensagem)
    intencao = classificacao.get("intencao", "outro")
    dados = classificacao.get("dados", {})

    contexto = ""

    if intencao == "registrar_conta":
        if dados.get("valor") and dados.get("descricao"):
            vencimento = dados.get("vencimento", date.today().isoformat())
            conta = db.criar_conta(
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                vencimento=vencimento,
                fornecedor=dados.get("fornecedor"),
                categoria=dados.get("categoria"),
            )
            contexto = f"Conta registrada com sucesso: {json.dumps(conta, ensure_ascii=False, default=str)}"
        else:
            contexto = "O usuário quer registrar uma conta mas faltam dados (valor e/ou descrição). Peça os dados que faltam."

    elif intencao == "registrar_gasto":
        if dados.get("valor") and dados.get("descricao"):
            data_gasto = dados.get("data", date.today().isoformat())
            gasto = db.criar_gasto(
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                data_gasto=data_gasto,
                categoria=dados.get("categoria"),
            )
            contexto = f"Gasto registrado com sucesso: {json.dumps(gasto, ensure_ascii=False, default=str)}"
        else:
            contexto = "O usuário quer registrar um gasto mas faltam dados. Peça os dados que faltam."

    elif intencao == "registrar_aluguel":
        if dados.get("valor") and dados.get("imovel"):
            vencimento = dados.get("vencimento", date.today().isoformat())
            aluguel = db.criar_aluguel(
                imovel=dados["imovel"],
                valor=float(dados["valor"]),
                vencimento=vencimento,
                locatario=dados.get("locatario"),
            )
            contexto = f"Aluguel registrado com sucesso: {json.dumps(aluguel, ensure_ascii=False, default=str)}"
        else:
            contexto = "O usuário quer registrar um aluguel mas faltam dados. Peça os dados que faltam."

    elif intencao == "cadastrar_fornecedor":
        if dados.get("fornecedor"):
            fornecedor = db.criar_fornecedor(
                nome=dados["fornecedor"],
                contato=dados.get("contato"),
                categoria=dados.get("categoria"),
            )
            contexto = f"Fornecedor cadastrado: {json.dumps(fornecedor, ensure_ascii=False, default=str)}"
        else:
            contexto = "O usuário quer cadastrar um fornecedor mas não informou o nome."

    elif intencao == "consultar_contas":
        contas = db.contas_proximas_vencimento(30)
        if contas:
            lista = "\n".join(
                f"• {c['descricao']} — R$ {c['valor']:.2f} — vence {c['vencimento']}"
                for c in contas
            )
            contexto = f"Contas pendentes próximas do vencimento:\n{lista}"
        else:
            contexto = "Não há contas pendentes próximas do vencimento."

    elif intencao == "consultar_gastos":
        periodo = dados.get("periodo", "mes")
        total = db.total_gastos(periodo)
        gastos = db.listar_gastos(periodo)
        if gastos:
            lista = "\n".join(
                f"• {g['descricao']} — R$ {g['valor']:.2f} ({g.get('categoria', 'sem categoria')})"
                for g in gastos[:10]
            )
            contexto = f"Total de gastos no período ({periodo}): R$ {total:.2f}\nÚltimos gastos:\n{lista}"
        else:
            contexto = f"Nenhum gasto registrado no período ({periodo})."

    elif intencao == "consultar_fornecedores":
        fornecedores = db.listar_fornecedores()
        if fornecedores:
            lista = "\n".join(f"• {f['nome']} ({f.get('categoria', 'sem categoria')})" for f in fornecedores)
            contexto = f"Fornecedores cadastrados:\n{lista}"
        else:
            contexto = "Nenhum fornecedor cadastrado ainda."

    elif intencao == "consultar_alugueis":
        alugueis = db.listar_alugueis()
        if alugueis:
            lista = "\n".join(
                f"• {a['imovel']} — R$ {a['valor']:.2f} — vence {a['vencimento']} ({a.get('status', '')})"
                for a in alugueis
            )
            contexto = f"Aluguéis:\n{lista}"
        else:
            contexto = "Nenhum aluguel registrado."

    elif intencao == "resumo_financeiro":
        periodo = dados.get("periodo", "mes")
        resumo = db.resumo_financeiro(periodo)
        contexto = f"""Resumo financeiro ({periodo}):
• Total de gastos: R$ {resumo['total_gastos']:.2f} ({resumo['quantidade_gastos']} registros)
• Contas pendentes: {resumo['contas_pendentes']} (R$ {resumo['total_contas_pendentes']:.2f})
• Aluguéis pendentes: {resumo['alugueis_pendentes']} (R$ {resumo['total_alugueis_pendentes']:.2f})
• Gastos por categoria: {json.dumps(resumo['gastos_por_categoria'], ensure_ascii=False)}
"""
        proximas = resumo.get("proximas_vencimento", [])
        if proximas:
            lista = "\n".join(f"  ⚠️ {c['descricao']} — R$ {c['valor']:.2f} — {c['vencimento']}" for c in proximas)
            contexto += f"\nContas vencendo nos próximos 7 dias:\n{lista}"

    elif intencao == "dica_financeira":
        try:
            resumo = db.resumo_financeiro("mes")
            contexto = f"Dados do usuário para basear a dica — gastos do mês: R$ {resumo['total_gastos']:.2f}, contas pendentes: {resumo['contas_pendentes']}"
        except Exception:
            contexto = "Dê uma dica financeira geral."

    # Gera a resposta conversacional
    resposta = gerar_resposta(mensagem, contexto)

    # Salva a conversa
    try:
        db.salvar_conversa(telefone, mensagem, resposta)
    except Exception:
        pass  # Não falha se não conseguir salvar o log

    return resposta
