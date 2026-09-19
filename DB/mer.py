"""Desenha o MER (modelo entidade-relacionamento) do banco em PNG.

O diagrama é gerado a partir de `DB/esquema.py`, a mesma especificação que
produz o DDL — então ele não sai de sincronia com o banco de verdade.

Uso:
    python -m DB.mer
    python -m DB.mer --saida DB/mer_solarwatch.png --dpi 200
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

from DB.esquema import TABELAS

log = logging.getLogger("DB.mer")

SAIDA = Path(__file__).resolve().parent / "mer_solarwatch.png"

# Layout: dimensão à esquerda, fatos à direita, ponte embaixo
POSICOES = {
    "dim_usina": {"x": 3, "y_topo": 97, "largura": 33, "cor": "#1f4e79", "papel": "DIMENSÃO"},
    "fato_geracao": {"x": 58, "y_topo": 97, "largura": 39, "cor": "#1d6f42", "papel": "FATO"},
    "fato_clima": {"x": 58, "y_topo": 71, "largura": 39, "cor": "#1d6f42", "papel": "FATO"},
    "fato_manutencao": {"x": 58, "y_topo": 33, "largura": 39, "cor": "#8a5a00", "papel": "FATO (simulado)"},
    "ponte_usina_aneel": {"x": 3, "y_topo": 24, "largura": 33, "cor": "#5a5a5a", "papel": "PONTE"},
}
ALTURA_CABECALHO = 5.2
ALTURA_LINHA = 2.45
COR_FUNDO_LINHA = "#f5f7fa"
COR_FUNDO_PK = "#e6eef7"


def _desenhar_tabela(ax, nome: str) -> dict:
    """Desenha uma tabela e devolve a posição vertical de cada coluna."""
    tabela, pos = TABELAS[nome], POSICOES[nome]
    x, y_topo, largura, cor = pos["x"], pos["y_topo"], pos["largura"], pos["cor"]
    colunas = tabela["colunas"]
    altura = ALTURA_CABECALHO + len(colunas) * ALTURA_LINHA
    y_base = y_topo - altura

    ax.add_patch(FancyBboxPatch(
        (x, y_base), largura, altura, boxstyle="round,pad=0,rounding_size=0.6",
        linewidth=1.4, edgecolor=cor, facecolor="white", zorder=2,
    ))
    # Cabeçalho: nome da tabela + grão
    ax.add_patch(Rectangle((x, y_topo - ALTURA_CABECALHO), largura, ALTURA_CABECALHO,
                           facecolor=cor, edgecolor=cor, zorder=3))
    ax.text(x + 0.8, y_topo - 2.1, nome, color="white", fontsize=11.5,
            fontweight="bold", va="center", zorder=4)
    ax.text(x + largura - 0.8, y_topo - 2.1, pos["papel"], color="white", fontsize=7.5,
            va="center", ha="right", zorder=4, style="italic")
    ax.text(x + 0.8, y_topo - 4.1, f"grão: {tabela['grao']}", color="white", fontsize=7.5,
            va="center", zorder=4)

    posicoes_colunas = {}
    for i, (col, tipo, obrigatoria, _) in enumerate(colunas):
        y = y_topo - ALTURA_CABECALHO - (i + 0.5) * ALTURA_LINHA
        posicoes_colunas[col] = y
        e_pk, e_fk = col in tabela["pk"], col in tabela["fk"]
        if e_pk or e_fk:
            fundo = COR_FUNDO_PK
        else:
            fundo = COR_FUNDO_LINHA if i % 2 else "white"
        ax.add_patch(Rectangle((x, y - ALTURA_LINHA / 2), largura, ALTURA_LINHA,
                               facecolor=fundo, edgecolor="none", zorder=2.5))
        marca = "PK" if e_pk and not e_fk else ("PK,FK" if e_pk and e_fk else ("FK" if e_fk else ""))
        ax.text(x + 0.8, y, marca, fontsize=6.5, va="center", color=cor,
                fontweight="bold", zorder=4)
        ax.text(x + 5.2, y, col, fontsize=8, va="center", zorder=4,
                fontweight="bold" if e_pk else "normal")
        ax.text(x + largura - 0.8, y, tipo + ("" if obrigatoria else " ·"), fontsize=7,
                va="center", ha="right", color="#666666", zorder=4)

    ax.plot([x, x + largura], [y_topo - ALTURA_CABECALHO] * 2, color=cor, lw=1.0, zorder=4)
    return posicoes_colunas


def _desenhar_relacao(ax, origem: tuple[float, float], destino: tuple[float, float],
                      x_cotovelo: float, cor: str = "#333333", lado: str = "esquerda") -> None:
    """Liga dim (lado 1) a fato (lado N) com um cotovelo e a cardinalidade.

    `lado` diz em qual borda da tabela N a linha chega, para o pé-de-galinha e o
    rótulo ficarem fora da caixa.
    """
    (x1, y1), (x2, y2) = origem, destino
    ax.plot([x1, x_cotovelo, x_cotovelo, x2], [y1, y1, y2, y2],
            color=cor, lw=1.2, zorder=1, solid_joinstyle="round")
    ax.plot([x1], [y1], marker="o", ms=4, color=cor, zorder=3)
    ax.text(x1 + 1.2, y1 + 1.2, "1", fontsize=9, fontweight="bold", color=cor, zorder=4)
    sentido = 1 if lado == "esquerda" else -1  # para onde o pé-de-galinha aponta
    for dy in (-1.1, 0, 1.1):
        ax.plot([x2 - 2.2 * sentido, x2], [y2, y2 + dy], color=cor, lw=1.1, zorder=3)
    ax.text(x2 - 3.4 * sentido, y2 + 1.6, "N", fontsize=9, fontweight="bold",
            color=cor, ha="center", zorder=4)


def desenhar(saida: Path = SAIDA, dpi: int = 200) -> Path:
    fig, ax = plt.subplots(figsize=(17, 12))
    ax.set_xlim(0, 100)
    ax.set_ylim(-10, 104)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    colunas = {nome: _desenhar_tabela(ax, nome) for nome in TABELAS}

    x_dim_dir = POSICOES["dim_usina"]["x"] + POSICOES["dim_usina"]["largura"]
    y_pk_dim = colunas["dim_usina"]["usina_id"]
    for i, fato in enumerate(["fato_geracao", "fato_clima", "fato_manutencao"]):
        _desenhar_relacao(ax, (x_dim_dir, y_pk_dim),
                          (POSICOES[fato]["x"], colunas[fato]["usina_id"]),
                          x_cotovelo=41 + i * 4.5)
    # ponte: liga por baixo da dimensão
    _desenhar_relacao(ax, (x_dim_dir, y_pk_dim),
                      (POSICOES["ponte_usina_aneel"]["x"] + POSICOES["ponte_usina_aneel"]["largura"],
                       colunas["ponte_usina_aneel"]["usina_id"]),
                      x_cotovelo=54, cor="#777777", lado="direita")

    ax.text(3, 103, "SolarWatch BR — Modelo Entidade-Relacionamento (DuckDB)",
            fontsize=17, fontweight="bold", va="top")
    ax.text(3, 100.2,
            "Esquema estrela: dim_usina (unidade geradora solar/eólica medida pelo ONS) + 3 fatos. "
            "Gerado de DB/esquema.py, a mesma especificação que cria o banco.",
            fontsize=9, va="top", color="#444444")
    ax.text(58, 6.0,
            "Legenda:  PK = chave primária · FK = chave estrangeira · '·' = coluna aceita nulo\n"
            "Cardinalidade 1:N em todas as relações (fato_manutencao tem no máximo 1 linha por usina).\n"
            "fato_manutencao contém dados SIMULADOS (ML/analise_sobrevivencia/dados_simulados.py).\n"
            "ponte_usina_aneel é auditoria do vínculo ONS × ANEEL, não é consumida pela API.",
            fontsize=8.5, va="top", color="#444444",
            bbox=dict(boxstyle="round,pad=0.6", facecolor="#f5f7fa", edgecolor="#cccccc"))

    saida.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(saida, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    log.info("[mer] gravado %s (%.0f KB)", saida, saida.stat().st_size / 1024)
    return saida


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--saida", type=Path, default=SAIDA)
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()
    desenhar(args.saida, args.dpi)


if __name__ == "__main__":
    main()
