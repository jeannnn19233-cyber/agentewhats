import os
from datetime import date, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===================== CONTAS A PAGAR =====================

def criar_conta(descricao: str, valor: float, vencimento: str,
                fornecedor: str | None = None, categoria: str | None = None) -> dict:
    data = {
        "descricao": descricao,
        "valor": valor,
        "vencimento": vencimento,
        "status": "pendente",
    }
    if fornecedor:
        data["fornecedor"] = fornecedor
    if categoria:
        data["categoria"] = categoria

    result = supabase.table("contas_pagar").insert(data).execute()
    return result.data[0] if result.data else {}


def listar_contas(status: str | None = None) -> list[dict]:
    query = supabase.table("contas_pagar").select("*").order("vencimento")
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return result.data or []


def contas_proximas_vencimento(dias: int = 7) -> list[dict]:
    hoje = date.today().isoformat()
    limite = (date.today() + timedelta(days=dias)).isoformat()
    result = (
        supabase.table("contas_pagar")
        .select("*")
        .eq("status", "pendente")
        .gte("vencimento", hoje)
        .lte("vencimento", limite)
        .order("vencimento")
        .execute()
    )
    return result.data or []


def marcar_conta_paga(conta_id: int) -> dict:
    result = (
        supabase.table("contas_pagar")
        .update({"status": "pago"})
        .eq("id", conta_id)
        .execute()
    )
    return result.data[0] if result.data else {}


# ===================== FORNECEDORES =====================

def criar_fornecedor(nome: str, contato: str | None = None,
                     categoria: str | None = None) -> dict:
    data = {"nome": nome}
    if contato:
        data["contato"] = contato
    if categoria:
        data["categoria"] = categoria

    result = supabase.table("fornecedores").insert(data).execute()
    return result.data[0] if result.data else {}


def listar_fornecedores() -> list[dict]:
    result = supabase.table("fornecedores").select("*").order("nome").execute()
    return result.data or []


# ===================== GASTOS PESSOAIS =====================

def criar_gasto(descricao: str, valor: float, data_gasto: str,
                categoria: str | None = None) -> dict:
    data = {
        "descricao": descricao,
        "valor": valor,
        "data": data_gasto,
    }
    if categoria:
        data["categoria"] = categoria

    result = supabase.table("gastos_pessoais").insert(data).execute()
    return result.data[0] if result.data else {}


def listar_gastos(periodo: str = "mes") -> list[dict]:
    hoje = date.today()
    if periodo == "semana":
        inicio = (hoje - timedelta(days=hoje.weekday())).isoformat()
    elif periodo == "ano":
        inicio = date(hoje.year, 1, 1).isoformat()
    else:  # mes
        inicio = date(hoje.year, hoje.month, 1).isoformat()

    result = (
        supabase.table("gastos_pessoais")
        .select("*")
        .gte("data", inicio)
        .order("data", desc=True)
        .execute()
    )
    return result.data or []


def total_gastos(periodo: str = "mes") -> float:
    gastos = listar_gastos(periodo)
    return sum(g["valor"] for g in gastos)


# ===================== ALUGUÉIS =====================

def criar_aluguel(imovel: str, valor: float, vencimento: str,
                  locatario: str | None = None) -> dict:
    data = {
        "imovel": imovel,
        "valor": valor,
        "vencimento": vencimento,
        "status": "pendente",
    }
    if locatario:
        data["locatario"] = locatario

    result = supabase.table("alugueis").insert(data).execute()
    return result.data[0] if result.data else {}


def listar_alugueis(status: str | None = None) -> list[dict]:
    query = supabase.table("alugueis").select("*").order("vencimento")
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return result.data or []


# ===================== CONVERSAS =====================

def salvar_conversa(telefone: str, mensagem: str, resposta: str) -> dict:
    data = {
        "telefone": telefone,
        "mensagem": mensagem,
        "resposta": resposta,
    }
    result = supabase.table("conversas").insert(data).execute()
    return result.data[0] if result.data else {}


# ===================== RESUMO FINANCEIRO =====================

def resumo_financeiro(periodo: str = "mes") -> dict:
    gastos = listar_gastos(periodo)
    contas = listar_contas()
    alugueis = listar_alugueis()

    total_g = sum(g["valor"] for g in gastos)
    contas_pendentes = [c for c in contas if c.get("status") == "pendente"]
    total_contas_pendentes = sum(c["valor"] for c in contas_pendentes)
    total_alugueis = sum(a["valor"] for a in alugueis if a.get("status") == "pendente")

    # Agrupar gastos por categoria
    categorias: dict[str, float] = {}
    for g in gastos:
        cat = g.get("categoria") or "Sem categoria"
        categorias[cat] = categorias.get(cat, 0) + g["valor"]

    return {
        "total_gastos": total_g,
        "quantidade_gastos": len(gastos),
        "gastos_por_categoria": categorias,
        "contas_pendentes": len(contas_pendentes),
        "total_contas_pendentes": total_contas_pendentes,
        "alugueis_pendentes": len([a for a in alugueis if a.get("status") == "pendente"]),
        "total_alugueis_pendentes": total_alugueis,
        "proximas_vencimento": contas_proximas_vencimento(7),
    }
