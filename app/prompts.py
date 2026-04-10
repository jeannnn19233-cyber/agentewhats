import os as _os
from pathlib import Path as _Path

def _load_brain() -> str:
    """Carrega o brain.md como system prompt."""
    brain_path = _Path(__file__).parent / "brain.md"
    if brain_path.exists():
        return brain_path.read_text(encoding="utf-8")
    return "Você é a Maria, assistente financeira da Evolution Financeiro."

SYSTEM_PROMPT = _load_brain()

VISION_PROMPT = """Analise esta imagem e extraia os dados do boleto bancário brasileiro em JSON.

Retorne APENAS o JSON abaixo, sem texto adicional, sem markdown:

{
  "valor": <número decimal, ex: 150.00 — null se não legível>,
  "vencimento": <string "DD/MM/AAAA" — null se não legível>,
  "beneficiario": <nome do beneficiário/cedente — null se não legível>,
  "linha_digitavel": <linha digitável se visível — null se não visível>,
  "descricao": <descrição breve do que é o boleto — null se não identificável>
}

Regras:
- Use null para campos não legíveis ou ausentes
- O valor deve ser um número puro (ex: 150.00), sem R$ ou pontuação
- A data deve estar no formato DD/MM/AAAA
- Se a imagem não for um boleto, retorne todos os campos como null
"""

INTENT_PROMPT = """Analise a mensagem do usuário e classifique a intenção.
Use o HISTÓRICO DA CONVERSA e o CONTEXTO ATIVO para entender o que o usuário quer.

Regras de classificação:

**Onboarding (cadastro inicial):**
- Se o contexto indicar que o onboarding NÃO está completo, qualquer mensagem com nome, tipo de uso (pessoal/empresarial), CNPJ ou orçamento deve ser classificada como "onboarding"
- Se o onboarding não está completo e o usuário tentar fazer outra coisa, classifique como "onboarding" (a Maria vai redirecionar)

**Confirmação / cancelamento:**
- "sim", "ok", "pode", "confirma", "isso", "correto", "manda ver", "salva", "tá certo", "certo", "vai", "tá bom", "pode ser" → "confirmar" (SOMENTE se o histórico mostrar pedido de confirmação pendente)
- "não", "cancela", "errado", "deixa pra lá", "espera", "para" → "cancelar" (SOMENTE se houver pedido de confirmação no histórico)

**Complemento de dados em fluxo multi-etapa:**
- Se a mensagem for apenas um número e a Maria estava pedindo valor → classifique como o registro em andamento
- Se a mensagem for uma data e a Maria estava pedindo vencimento → preencha esse campo
- Se a mensagem for um nome curto e a Maria estava pedindo descrição/fornecedor → preencha
- NUNCA classifique como "outro" quando houver fluxo de coleta em andamento

**Registros:**
- Conta a pagar / boleto / fatura / mensalidade → "registrar_conta"
- Gasto / compra / despesa / paguei / gastei → "registrar_gasto"
- Receita / entrada / recebi / vendi / faturei → "registrar_receita"
- Aluguel / imóvel → "registrar_aluguel"
- Fornecedor / empresa parceira → "cadastrar_fornecedor"

**Consultas:**
- Contas / boletos / vencimento → "consultar_contas"
- Quanto gastei / gastos / despesas → "consultar_gastos"
- Receitas / entradas / quanto recebi → "consultar_receitas"
- Fluxo de caixa / saldo / balanço → "fluxo_caixa"
- Fornecedores → "consultar_fornecedores"
- Aluguéis → "consultar_alugueis"
- Resumo / extrato → "resumo_financeiro"
- Dica / conselho / sugestão → "dica_financeira"

**Exclusões / apagar:**
- Apagar gasto / excluir despesa / remover gasto → "apagar_gasto"
- Apagar conta / excluir boleto / remover conta → "apagar_conta"
- Apagar receita / excluir entrada / remover receita → "apagar_receita"
- Apagar fornecedor / remover fornecedor → "apagar_fornecedor"
- Marcar pago / já paguei / pago / quitei → "marcar_pago"
- Apagar tudo / resetar / recomeçar / limpar dados / zerar conta → "resetar_conta"

**Gráficos:**
- Gráfico de contas / fornecedor → "grafico_fornecedores"
- Gráfico de fluxo / receita vs gasto → "grafico_receita_gastos"
- Gráfico de categorias / pizza de gastos → "grafico_categorias"

**Perfil do usuário (após onboarding):**
- "me chamo / meu nome é" + nome → "configurar_perfil" com nome
- "uso empresarial / uso pessoal" → "configurar_perfil" com tipo
- "meu orçamento é" + valor → "configurar_perfil" com orcamento_mensal
- "atualizar perfil / meus dados" → "configurar_perfil"

**Categorização automática:**
- almoço / jantar / lanche / restaurante / ifood / delivery → alimentação
- uber / 99 / taxi / gasolina / combustível / estacionamento → transporte
- médico / farmácia / remédio / plano de saúde → saúde
- escola / curso / livro / faculdade → educação
- roupa / sapato / vestuário → vestuário
- netflix / cinema / show / jogo → lazer
- aluguel / condomínio / água / luz / internet → moradia
- venda / serviço prestado / freelance → receita de serviço
- salário / pró-labore → receita de trabalho

Retorne APENAS um JSON válido, sem markdown:

{
  "intencao": "onboarding" | "confirmar" | "cancelar" | "registrar_conta" | "registrar_gasto" | "registrar_receita" | "registrar_aluguel" | "cadastrar_fornecedor" | "consultar_contas" | "consultar_gastos" | "consultar_receitas" | "consultar_fornecedores" | "consultar_alugueis" | "resumo_financeiro" | "fluxo_caixa" | "dica_financeira" | "grafico_fornecedores" | "grafico_receita_gastos" | "grafico_categorias" | "configurar_perfil" | "apagar_gasto" | "apagar_conta" | "apagar_receita" | "apagar_fornecedor" | "marcar_pago" | "resetar_conta" | "saudacao" | "outro",
  "dados": {
    "descricao": <string ou null>,
    "valor": <número ou null>,
    "vencimento": <"AAAA-MM-DD" ou null>,
    "data": <"AAAA-MM-DD" ou null>,
    "categoria": <string ou null>,
    "fornecedor": <string ou null>,
    "imovel": <string ou null>,
    "locatario": <string ou null>,
    "periodo": "semana" | "mes" | "ano" | null,
    "nome": <string ou null>,
    "tipo": "pessoal" | "empresarial" | null,
    "orcamento_mensal": <número ou null>,
    "cnpj": <string somente dígitos ou null>
  }
}

CONTEXTO ATIVO:
{contexto_ativo}

HISTÓRICO DA CONVERSA (mais antigo → mais recente):
{historico}

Mensagem atual do usuário: {mensagem}
"""
