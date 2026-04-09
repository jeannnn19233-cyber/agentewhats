"""
Agendador de tarefas periódicas.
Registrado no lifespan do FastAPI (main.py).
"""
import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import database as db
from app.evolution import enviar_mensagem

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def _fmt_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def _notificar_usuario(telefone: str, contas: list[dict]) -> None:
    """Monta e envia a mensagem de alerta de vencimento para um usuário."""
    usuario = db.obter_ou_criar_usuario(telefone)
    nome = usuario.get("nome")
    saudacao = f"Oi, {nome}! 👋" if nome else "Olá! 👋"

    hoje = date.today()
    linhas = [f"{saudacao} Lembrete de contas próximas do vencimento:\n"]

    for c in contas:
        venc = date.fromisoformat(c["vencimento"])
        dias = (venc - hoje).days
        if dias == 0:
            prazo = "vence *hoje*‼️"
        elif dias == 1:
            prazo = "vence *amanhã* ⚠️"
        else:
            prazo = f"vence em *{dias} dias*"
        linhas.append(f"• {c['descricao']} — {_fmt_brl(c['valor'])} — {prazo}")

    total = sum(c["valor"] for c in contas)
    linhas.append(f"\nTotal: *{_fmt_brl(total)}*")
    linhas.append("\nResponda qualquer coisa para marcar como pago ou pedir ajuda. 💬")

    await enviar_mensagem(telefone, "\n".join(linhas))
    logger.info("[scheduler] alerta enviado para %s (%d contas)", telefone, len(contas))


async def job_alertas_vencimento() -> None:
    """
    Roda diariamente às 8h (horário de Brasília).
    Busca contas vencendo em até 3 dias e notifica cada usuário.
    """
    hoje = date.today()
    limite = hoje + timedelta(days=3)

    try:
        result = (
            db.supabase.table("contas_pagar")
            .select("telefone, descricao, valor, vencimento")
            .eq("status", "pendente")
            .gte("vencimento", hoje.isoformat())
            .lte("vencimento", limite.isoformat())
            .order("vencimento")
            .execute()
        )
    except Exception as e:
        logger.error("[scheduler] erro ao buscar contas: %s", e, exc_info=True)
        return

    # Agrupa por telefone
    por_telefone: dict[str, list[dict]] = {}
    for conta in result.data or []:
        t = conta["telefone"]
        por_telefone.setdefault(t, []).append(conta)

    if not por_telefone:
        logger.info("[scheduler] nenhuma conta vencendo nos próximos 3 dias")
        return

    logger.info("[scheduler] notificando %d usuário(s)", len(por_telefone))
    for telefone, contas in por_telefone.items():
        try:
            await _notificar_usuario(telefone, contas)
        except Exception as e:
            logger.error("[scheduler] falha ao notificar %s: %s", telefone, e, exc_info=True)


# Registra o job: todo dia às 8h no horário de Brasília
scheduler.add_job(
    job_alertas_vencimento,
    CronTrigger(hour=8, minute=0, timezone="America/Sao_Paulo"),
    id="alertas_vencimento",
    replace_existing=True,
    misfire_grace_time=300,  # 5 min de tolerância se o servidor estiver ocupado
)
