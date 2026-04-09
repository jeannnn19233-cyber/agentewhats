"""
Geração de gráficos financeiros para envio via WhatsApp.
Todas as funções retornam (base64_png: str, caption: str).
"""
import io
import base64
from calendar import month_abbr
from datetime import date, timedelta

import matplotlib
matplotlib.use("Agg")  # backend sem display — obrigatório em Docker/Linux headless
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker


# ── Paleta e estilo ──────────────────────────────────────────────────────────

CORES_PRIMARIAS = ["#7c3aed", "#2563eb", "#059669", "#d97706", "#dc2626",
                   "#7c3aed", "#0891b2", "#65a30d", "#9333ea", "#ea580c"]

def _estilo():
    plt.rcParams.update({
        "figure.facecolor": "#ffffff",
        "axes.facecolor": "#f8fafc",
        "axes.edgecolor": "#cbd5e1",
        "axes.labelcolor": "#334155",
        "xtick.color": "#64748b",
        "ytick.color": "#64748b",
        "text.color": "#1e293b",
        "grid.color": "#e2e8f0",
        "grid.linewidth": 0.8,
        "font.family": "DejaVu Sans",
        "font.size": 10,
    })


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    resultado = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return resultado


def _fmt_brl(valor: float) -> str:
    """Formata número como R$ 1.234,56"""
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_brl_curto(valor: float) -> str:
    """Versão curta para eixos: 1.2k, 45k, 1.2M"""
    if valor >= 1_000_000:
        return f"{valor/1_000_000:.1f}M"
    if valor >= 1_000:
        return f"{valor/1_000:.1f}k"
    return f"{valor:.0f}"


# ── Gráfico 1: Contas a pagar por fornecedor ────────────────────────────────

def grafico_contas_por_fornecedor(contas: list[dict]) -> tuple[str, str]:
    """
    Barras horizontais com o valor total de contas pendentes por fornecedor.
    Entrada: lista de dicts com 'fornecedor' e 'valor'.
    """
    _estilo()

    # Agrupa por fornecedor
    fornecedores: dict[str, float] = {}
    for c in contas:
        nome = (c.get("fornecedor") or "Sem fornecedor").strip()
        fornecedores[nome] = fornecedores.get(nome, 0.0) + float(c["valor"])

    # Ordena do maior para menor, limita a 12 para legibilidade
    itens = sorted(fornecedores.items(), key=lambda x: x[1], reverse=True)[:12]
    nomes = [i[0] for i in itens]
    valores = [i[1] for i in itens]
    total = sum(valores)

    altura = max(3.5, len(nomes) * 0.55 + 1.2)
    fig, ax = plt.subplots(figsize=(8, altura))

    bars = ax.barh(nomes, valores, color=CORES_PRIMARIAS[0], height=0.6)

    # Rótulos com valor dentro/fora da barra
    for bar, val in zip(bars, valores):
        largura = bar.get_width()
        ax.text(
            largura + total * 0.01, bar.get_y() + bar.get_height() / 2,
            _fmt_brl(val), va="center", ha="left", fontsize=9, color="#334155"
        )

    ax.set_xlabel("Valor pendente")
    ax.set_title("📋 Contas a Pagar por Fornecedor", fontsize=12, fontweight="bold", pad=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: _fmt_brl_curto(v)))
    ax.set_xlim(0, max(valores) * 1.25)
    ax.invert_yaxis()
    ax.grid(axis="x", linestyle="--", alpha=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    caption = f"Contas a pagar por fornecedor • Total pendente: {_fmt_brl(total)}"
    return _fig_to_b64(fig), caption


# ── Gráfico 2: Receita vs Gastos ─────────────────────────────────────────────

def grafico_receita_vs_gastos(
    receitas: list[dict],
    gastos: list[dict],
    periodo: str = "mes",
) -> tuple[str, str]:
    """
    Barras agrupadas por mês/semana: receitas (verde) vs gastos (vermelho).
    Entrada: listas de dicts com 'data' (AAAA-MM-DD) e 'valor'.
    """
    _estilo()

    def _agrupar_por_mes(itens: list[dict]) -> dict[str, float]:
        resultado: dict[str, float] = {}
        for item in itens:
            dt = item.get("data", "")[:7]  # "AAAA-MM"
            resultado[dt] = resultado.get(dt, 0.0) + float(item["valor"])
        return resultado

    rec_mes = _agrupar_por_mes(receitas)
    gas_mes = _agrupar_por_mes(gastos)

    # Todos os períodos presentes
    periodos = sorted(set(rec_mes) | set(gas_mes))

    if not periodos:
        periodos = [date.today().strftime("%Y-%m")]

    labels = [f"{month_abbr[int(p[5:7])]}/{p[2:4]}" for p in periodos]
    vals_rec = [rec_mes.get(p, 0.0) for p in periodos]
    vals_gas = [gas_mes.get(p, 0.0) for p in periodos]
    saldos = [r - g for r, g in zip(vals_rec, vals_gas)]

    x = range(len(labels))
    largura = 0.35

    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 1.1 + 2), 5))

    bars_rec = ax.bar([i - largura / 2 for i in x], vals_rec, largura,
                      label="Receitas", color="#059669", alpha=0.85)
    bars_gas = ax.bar([i + largura / 2 for i in x], vals_gas, largura,
                      label="Gastos", color="#dc2626", alpha=0.85)

    # Linha de saldo
    ax.plot(list(x), saldos, "o--", color="#7c3aed", linewidth=1.8,
            markersize=5, label="Saldo", zorder=5)

    # Rótulos nas barras
    for bar in bars_rec:
        if bar.get_height() > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(vals_rec + vals_gas) * 0.01,
                    _fmt_brl_curto(bar.get_height()), ha="center", va="bottom", fontsize=8, color="#059669")
    for bar in bars_gas:
        if bar.get_height() > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(vals_rec + vals_gas) * 0.01,
                    _fmt_brl_curto(bar.get_height()), ha="center", va="bottom", fontsize=8, color="#dc2626")

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_title("💰 Receitas vs Gastos", fontsize=12, fontweight="bold", pad=10)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: _fmt_brl_curto(v)))
    ax.legend(loc="upper left", framealpha=0.8)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    total_rec = sum(vals_rec)
    total_gas = sum(vals_gas)
    saldo_total = total_rec - total_gas
    sinal = "💚" if saldo_total >= 0 else "🔴"
    caption = (
        f"Receitas: {_fmt_brl(total_rec)} • Gastos: {_fmt_brl(total_gas)} • "
        f"Saldo: {sinal} {_fmt_brl(saldo_total)}"
    )
    return _fig_to_b64(fig), caption


# ── Gráfico 3: Pizza de gastos por categoria ─────────────────────────────────

def grafico_pizza_categorias(gastos: list[dict]) -> tuple[str, str]:
    """
    Pizza com proporção de gastos por categoria.
    Entrada: lista de dicts com 'categoria' e 'valor'.
    """
    _estilo()

    categorias: dict[str, float] = {}
    for g in gastos:
        cat = (g.get("categoria") or "Sem categoria").strip().capitalize()
        categorias[cat] = categorias.get(cat, 0.0) + float(g["valor"])

    # Agrupa categorias muito pequenas em "Outros" (< 3% do total)
    total = sum(categorias.values())
    principais = {k: v for k, v in categorias.items() if v / total >= 0.03}
    outros = total - sum(principais.values())
    if outros > 0:
        principais["Outros"] = outros

    labels = list(principais.keys())
    valores = list(principais.values())

    fig, ax = plt.subplots(figsize=(7, 6))
    wedges, texts, autotexts = ax.pie(
        valores,
        labels=None,
        autopct="%1.1f%%",
        startangle=90,
        colors=CORES_PRIMARIAS[: len(valores)],
        wedgeprops={"edgecolor": "white", "linewidth": 1.5},
        pctdistance=0.82,
    )

    for t in autotexts:
        t.set_fontsize(8)
        t.set_color("white")
        t.set_fontweight("bold")

    # Legenda externa com valores
    legenda_labels = [f"{l} — {_fmt_brl(v)}" for l, v in zip(labels, valores)]
    ax.legend(wedges, legenda_labels, loc="lower center", bbox_to_anchor=(0.5, -0.18),
              ncol=2, fontsize=9, framealpha=0.8)

    ax.set_title("📊 Gastos por Categoria", fontsize=12, fontweight="bold", pad=10)
    fig.tight_layout()

    maior_cat = max(principais, key=principais.get)
    caption = (
        f"Gastos por categoria • Total: {_fmt_brl(total)} • "
        f"Maior gasto: {maior_cat} ({_fmt_brl(principais[maior_cat])})"
    )
    return _fig_to_b64(fig), caption
