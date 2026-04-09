# Agente Financeiro - WhatsApp + IA

Agente de IA para finanças pessoais e empresariais integrado ao WhatsApp.
Recebe mensagens de texto e fotos de boletos, registra dados e dá dicas financeiras.

## Stack

- **Backend:** Python 3.11 + FastAPI
- **IA:** OpenAI API (GPT-4o-mini para texto, GPT-4o para visão)
- **WhatsApp:** Evolution API (webhook)
- **Banco de dados:** Supabase (PostgreSQL)
- **Deploy:** Docker + Coolify

---

## 1. Configurar o Supabase

### Criar as tabelas

Execute o SQL abaixo no **SQL Editor** do Supabase:

```sql
-- Contas a Pagar
CREATE TABLE contas_pagar (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    descricao TEXT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    vencimento DATE NOT NULL,
    fornecedor TEXT,
    status TEXT DEFAULT 'pendente',
    categoria TEXT,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Fornecedores
CREATE TABLE fornecedores (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    nome TEXT NOT NULL,
    contato TEXT,
    categoria TEXT,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Gastos Pessoais
CREATE TABLE gastos_pessoais (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    descricao TEXT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    categoria TEXT,
    data DATE NOT NULL,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Aluguéis
CREATE TABLE alugueis (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    imovel TEXT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    vencimento DATE NOT NULL,
    locatario TEXT,
    status TEXT DEFAULT 'pendente',
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Conversas (log)
CREATE TABLE conversas (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    mensagem TEXT NOT NULL,
    resposta TEXT NOT NULL,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Perfil dos usuários
CREATE TABLE usuarios (
    telefone TEXT PRIMARY KEY,
    nome TEXT,
    tipo TEXT DEFAULT 'pessoal',
    orcamento_mensal DECIMAL(12,2),
    criado_em TIMESTAMPTZ DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ DEFAULT NOW()
);

-- Receitas / Entradas
CREATE TABLE receitas (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    descricao TEXT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    categoria TEXT,
    data DATE NOT NULL DEFAULT CURRENT_DATE,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON receitas (telefone, data);

-- Ações pendentes (aguardando confirmação do usuário)
CREATE TABLE pending_actions (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_data JSONB NOT NULL,
    preview TEXT DEFAULT '',
    criado_em TIMESTAMPTZ DEFAULT NOW()
);
```

### Migração (banco já existente)

Se você já criou as tabelas antes, rode este SQL para adicionar o que falta:

```sql
-- Adiciona coluna telefone nas tabelas que não tinham
ALTER TABLE contas_pagar ADD COLUMN IF NOT EXISTS telefone TEXT NOT NULL DEFAULT '';
ALTER TABLE fornecedores ADD COLUMN IF NOT EXISTS telefone TEXT NOT NULL DEFAULT '';
ALTER TABLE gastos_pessoais ADD COLUMN IF NOT EXISTS telefone TEXT NOT NULL DEFAULT '';
ALTER TABLE alugueis ADD COLUMN IF NOT EXISTS telefone TEXT NOT NULL DEFAULT '';

-- Tabelas novas
CREATE TABLE IF NOT EXISTS pending_actions (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_data JSONB NOT NULL,
    preview TEXT DEFAULT '',
    criado_em TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS receitas (
    id BIGSERIAL PRIMARY KEY,
    telefone TEXT NOT NULL,
    descricao TEXT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    categoria TEXT,
    data DATE NOT NULL DEFAULT CURRENT_DATE,
    criado_em TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS receitas_telefone_data ON receitas (telefone, data);

CREATE TABLE IF NOT EXISTS usuarios (
    telefone TEXT PRIMARY KEY,
    nome TEXT,
    tipo TEXT DEFAULT 'pessoal',
    orcamento_mensal DECIMAL(12,2),
    criado_em TIMESTAMPTZ DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ DEFAULT NOW()
);
```

### Pegar as credenciais

1. Vá em **Settings > API** no Supabase
2. Copie a **URL** e a **anon key** (ou service role key)

---

## 2. Configurar a Evolution API

### Instalar (se ainda não tiver)

A Evolution API pode rodar como container Docker na mesma VPS:

```yaml
# Adicione ao docker-compose ou instale via Coolify
services:
  evolution:
    image: atendai/evolution-api:latest
    ports:
      - "8080:8080"
    environment:
      - AUTHENTICATION_API_KEY=sua-chave-aqui
```

### Criar instância e conectar WhatsApp

1. Acesse a Evolution API (ex: `http://sua-vps:8080`)
2. Crie uma nova instância
3. Escaneie o QR Code com o WhatsApp
4. Configure o webhook apontando para:
   ```
   https://financeiro.ablcosmeticos.site/webhook
   ```
5. Eventos para escutar: `MESSAGES_UPSERT`

---

## 3. Variáveis de Ambiente

Crie um arquivo `.env` baseado no `.env.example`:

```bash
cp .env.example .env
```

Preencha:

| Variável | Descrição |
|----------|-----------|
| `OPENAI_API_KEY` | Chave da API OpenAI |
| `SUPABASE_URL` | URL do projeto Supabase |
| `SUPABASE_KEY` | Chave anon ou service_role |
| `EVOLUTION_API_URL` | URL da Evolution API (ex: http://localhost:8080) |
| `EVOLUTION_API_KEY` | API key da Evolution |
| `EVOLUTION_INSTANCE` | Nome da instância criada |
| `WEBHOOK_SECRET` | Segredo para validar webhooks (opcional) |

---

## 4. Rodar Localmente

### Com Python direto

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Com Docker

```bash
docker compose up --build
```

A API fica disponível em `http://localhost:8000`.

### Testar

- `GET /` — Status da aplicação
- `GET /health` — Health check
- `POST /webhook` — Endpoint que a Evolution API chama

---

## 5. Deploy no Coolify

### Passo a passo

1. **Suba o código para um repositório Git** (GitHub, GitLab, etc.)

2. **No Coolify**, clique em **New Resource > Application**

3. Configure:
   - **Source:** seu repositório Git
   - **Build Pack:** Dockerfile
   - **Port:** 8000
   - **Domain:** `financeiro.ablcosmeticos.site`

4. **Adicione as variáveis de ambiente** na aba Environment

5. **Deploy!**

### DNS

Aponte o subdomínio no seu provedor de DNS:

```
financeiro.ablcosmeticos.site → A → 72.61.46.130
```

O Coolify gera o certificado SSL automaticamente via Let's Encrypt.

---

## 6. Comandos que o agente entende

| Comando | O que faz |
|---------|-----------|
| Foto de boleto | Extrai dados e pergunta se quer registrar |
| "Registra esse boleto" + foto | Extrai e registra automaticamente |
| "Adiciona conta de R$500 vencendo dia 15" | Registra conta a pagar |
| "Gastei R$50 no almoço" | Registra gasto pessoal |
| "Quais contas vencem essa semana?" | Lista contas próximas |
| "Quanto gastei esse mês?" | Mostra total e detalhes |
| "Me dá um resumo financeiro" | Resumo completo |
| "Quais são meus fornecedores?" | Lista fornecedores |
| "Adiciona aluguel de R$1500 vencendo dia 10" | Registra aluguel |
| "Me dá uma dica financeira" | Dica baseada nos seus dados |

---

## Arquitetura

```
WhatsApp → Evolution API → Webhook (FastAPI) → OpenAI → Supabase
                                    ↓
                          Resposta via Evolution API → WhatsApp
```

## Estrutura

```
agente-financeiro/
├── app/
│   ├── main.py          # Entry point FastAPI
│   ├── webhook.py       # Recebe mensagens do WhatsApp
│   ├── agent.py         # Lógica do agente com OpenAI
│   ├── vision.py        # Leitura de boletos com GPT-4o
│   ├── database.py      # Conexão e queries Supabase
│   └── prompts.py       # System prompts do agente
├── models/
│   └── schemas.py       # Modelos Pydantic
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
