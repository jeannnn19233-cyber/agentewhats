from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional


@dataclass
class AgentResponse:
    """Resposta do agente — pode ser só texto, só imagem, ou os dois."""
    text: str
    image_b64: str | None = None   # PNG em base64 pronto para enviar
    image_caption: str = ""        # legenda exibida abaixo da imagem no WhatsApp
    buttons: list[dict] | None = None  # [{"id": "btn1", "text": "Texto"}] — WhatsApp Business


# --- Contas a Pagar ---

class ContaPagarCreate(BaseModel):
    descricao: str
    valor: float = Field(gt=0)
    vencimento: date
    fornecedor: Optional[str] = None
    categoria: Optional[str] = None

class ContaPagar(ContaPagarCreate):
    id: int
    status: str = "pendente"
    criado_em: datetime


# --- Fornecedores ---

class FornecedorCreate(BaseModel):
    nome: str
    contato: Optional[str] = None
    categoria: Optional[str] = None

class Fornecedor(FornecedorCreate):
    id: int
    criado_em: datetime


# --- Gastos Pessoais ---

class GastoPessoalCreate(BaseModel):
    descricao: str
    valor: float = Field(gt=0)
    categoria: Optional[str] = None
    data: date

class GastoPessoal(GastoPessoalCreate):
    id: int
    criado_em: datetime


# --- Aluguéis ---

class AluguelCreate(BaseModel):
    imovel: str
    valor: float = Field(gt=0)
    vencimento: date
    locatario: Optional[str] = None

class Aluguel(AluguelCreate):
    id: int
    status: str = "pendente"
    criado_em: datetime


# --- Conversas ---

class Conversa(BaseModel):
    id: int
    telefone: str
    mensagem: str
    resposta: str
    criado_em: datetime


# --- Webhook Evolution API ---

class WebhookMessage(BaseModel):
    instance: Optional[str] = None
    data: Optional[dict] = None
    event: Optional[str] = None


# --- Perfil do usuário ---

class UsuarioPerfil(BaseModel):
    telefone: str
    nome: Optional[str] = None
    tipo: str = "pessoal"              # "pessoal" | "empresarial"
    orcamento_mensal: Optional[float] = None
    criado_em: Optional[datetime] = None
    atualizado_em: Optional[datetime] = None


# --- Receitas ---

class ReceitaCreate(BaseModel):
    descricao: str
    valor: float = Field(gt=0)
    data: date
    categoria: Optional[str] = None


# --- Dados extraídos de boleto ---

class DadosBoleto(BaseModel):
    valor: Optional[float] = None
    vencimento: Optional[str] = None
    beneficiario: Optional[str] = None
    linha_digitavel: Optional[str] = None
    descricao: Optional[str] = None
