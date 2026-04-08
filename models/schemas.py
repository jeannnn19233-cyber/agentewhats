from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional


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


# --- Dados extraídos de boleto ---

class DadosBoleto(BaseModel):
    valor: Optional[float] = None
    vencimento: Optional[str] = None
    beneficiario: Optional[str] = None
    linha_digitavel: Optional[str] = None
    descricao: Optional[str] = None
