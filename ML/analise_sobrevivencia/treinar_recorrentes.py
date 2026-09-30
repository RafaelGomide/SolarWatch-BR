"""Eventos recorrentes de manutenção: Andersen-Gill, PWP e função média cumulativa.

Complementa `treinar.py`, que modela só o 1º evento. Aqui a usina continua sob
risco depois de cada reparo, que é como a operação realmente funciona.

Fluxo:

1. carrega o painel de episódios (`eventos_manutencao_recorrentes.parquet`);
2. ajusta **Andersen-Gill**, **PWP tempo total** e **PWP gap time**;
3. compara os coeficientes entre si e com os valores verdadeiros do gerador;
4. mostra o efeito do erro-padrão agrupado por usina;
5. estima a **MCF** (manutenções acumuladas por usina) por fonte;
6. estima a **fragilidade gama** por usina (heterogeneidade não observada);
7. **saída de produto**: manutenções esperadas por usina em 6, 12, 24 e 36 meses;
8. salva o previsor em pickle e as tabelas em Parquet.

Uso:
    python -m ML.analise_sobrevivencia.treinar_recorrentes
    python -m ML.analise_sobrevivencia.treinar_recorrentes --sem-graficos --sem-salvar
"""

from __future__ import annotations

import argparse
import json
import logging
import warnings
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import ds_toolkit as dst
from ML.analise_sobrevivencia import dados, recorrentes
from ML.analise_sobrevivencia.config import HORIZONTES_MESES, MODELOS, RESULTADOS
from ML.analise_sobrevivencia.dados_simulados import SAIDA_RECORRENTES

log = logging.getLogger("ML.analise_sobrevivencia.recorrentes")


def carregar_painel() -> tuple[pd.DataFrame, dict]:
    if not SAIDA_RECORRENTES.exists():
        raise FileNotFoundError(
            f"{SAIDA_RECORRENTES} não existe. Rode: "
            "python -m ML.analise_sobrevivencia.dados_simulados"
        )
    painel = pd.read_parquet(SAIDA_RECORRENTES)
    meta_json = SAIDA_RECORRENTES.with_name(SAIDA_RECORRENTES.stem + ".meta.json")
    meta = json.loads(meta_json.read_text(encoding="utf-8")) if meta_json.exists() else {}

    por_usina = painel.groupby("id_usina")["evento"].sum()
    log.info("[dados] %d episódios de %d usinas | %d eventos | %.0f%% das usinas com 2+ eventos",
             len(painel), painel["id_usina"].nunique(), int(painel["evento"].sum()),
             100 * (por_usina >= 2).mean())
    return recorrentes.preparar(painel), meta


def _grafico_mcf(mcf: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(8, 5))
    for fonte, grupo in mcf.groupby("fonte"):
        ax.step(grupo["tempo_anos"], grupo["mcf"], where="post", label=fonte, lw=2)
    ax.set(xlabel="Anos desde a entrada em operação",
           ylabel="Manutenções acumuladas por usina",
           title="Função média cumulativa (dado simulado)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    caminho = RESULTADOS / "mcf_recorrentes.png"
    RESULTADOS.mkdir(parents=True, exist_ok=True)
    fig.savefig(caminho, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return caminho


def executar(horizontes=HORIZONTES_MESES, graficos: bool = True, salvar: bool = True) -> dict:
    painel, meta = carregar_painel()

    # 1. Os três modelos ------------------------------------------------------
    ajustes = {
        "andersen_gill": recorrentes.ajustar_andersen_gill(painel),
        "pwp_tempo_total": recorrentes.ajustar_pwp_tempo_total(painel),
        "pwp_gap_time": recorrentes.ajustar_pwp_gap(painel),
    }
    comparacao = recorrentes.comparar(ajustes, meta.get("beta_verdadeiro"))
    erros_padrao = recorrentes.comparar_erros_padrao(painel)

    # 2. MCF e fragilidade ----------------------------------------------------
    mcf = recorrentes.funcao_media_cumulativa(painel)
    frailty = recorrentes.estimar_frailty_gama(painel, mcf)

    # 3. Produto --------------------------------------------------------------
    previsor = recorrentes.PrevisorRecorrencia(ajustes["andersen_gill"], mcf, horizontes, metadados={
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "origem_treino": SAIDA_RECORRENTES.name,
        "dado_simulado": True,
        "n_usinas_treino": int(painel["id_usina"].nunique()),
        "n_episodios": len(painel),
        "n_eventos": int(painel["evento"].sum()),
        "modelos_ajustados": list(ajustes),
        "beta_verdadeiro": meta.get("beta_verdadeiro"),
        "gamma_recorrencia": meta.get("gamma_recorrencia"),
        "max_estrato_episodio": recorrentes.MAX_ESTRATO,
        "frailty_theta_estimado": frailty["theta"],
        "frailty_theta_verdadeiro": meta.get("variancia_frailty"),
        "horizontes_meses": list(horizontes),
        "aproximacao": "MCF da fonte x exp(beta_AG . x); multiplicador sobre média marginal",
    })

    usinas = dados.carregar_usinas_para_previsao()
    previsoes = previsor.prever(usinas)
    colunas = ["usina_id", "nome", "fonte", "id_subsistema", "potencia_mw", "idade_anos",
               "taxa_relativa", *[f"manutencoes_esperadas_{m}m" for m in horizontes]]
    previsoes = previsoes[colunas]

    # 3. Relatório ------------------------------------------------------------
    print("\n=== Coeficientes: Andersen-Gill x PWP (x valor verdadeiro) ===")
    print(comparacao.round(3).to_string(index=False))
    if "ic_cobre" in comparacao:
        cobertura = comparacao.groupby("modelo")["ic_cobre"].mean().mul(100).round(0)
        print("\ncobertura dos IC 95% sobre o beta verdadeiro (%):")
        print(cobertura.to_string())
    print("\n=== Erro-padrão do AG: ingênuo x agrupado por usina ===")
    print(erros_padrao.round(4).to_string(index=False))
    print("\n=== Fragilidade gama por usina ===")
    verdadeiro = meta.get("variancia_frailty")
    print(f"theta estimado = {frailty['theta']:.3f} "
          f"(IC95 {frailty['ic_inferior']:.3f} a {frailty['ic_superior']:.3f})"
          + (f" | theta amostral do gerador = {verdadeiro:.3f}" if verdadeiro else ""))
    print(f"razao de verossimilhanca contra o Poisson (sem fragilidade) = "
          f"{frailty['lr_vs_poisson']:.1f}")
    por_usina = frailty["por_usina"]
    if "frailty" in painel.columns:
        real = painel.drop_duplicates("id_usina")[["id_usina", "frailty"]]
        confere = por_usina.merge(real, on="id_usina")
        print("correlacao entre a fragilidade estimada e a verdadeira: "
              f"Pearson {confere['frailty_posterior'].corr(confere['frailty']):.2f}, "
              f"Spearman {confere['frailty_posterior'].corr(confere['frailty'], method='spearman'):.2f}")
    print("usinas mais frageis do que as covariaveis explicam:")
    print(por_usina.nlargest(5, "frailty_posterior")
          [["id_usina", "fonte", "eventos", "exposicao", "frailty_posterior"]]
          .round(2).to_string(index=False))

    print("\n=== MCF por fonte (manutenções acumuladas por usina) ===")
    for fonte, grupo in mcf.groupby("fonte"):
        marcos = [(anos, float(grupo.loc[grupo["tempo_anos"] <= anos, "mcf"].max() or 0))
                  for anos in (1, 3, 5, 10)]
        print(f"  {fonte}: " + " | ".join(f"{a} ano(s): {v:.2f}" for a, v in marcos))
    print(f"\n=== Manutenções esperadas — {len(previsoes)} usinas ===")
    print(previsoes.groupby("fonte")[[f"manutencoes_esperadas_{m}m" for m in horizontes]]
          .mean().round(3).to_string())
    print(previsoes.nlargest(5, f"manutencoes_esperadas_{horizontes[1]}m").round(3).to_string(index=False))

    if graficos:
        log.info("[recorrentes] gráfico: %s", _grafico_mcf(mcf))

    if salvar:
        caminho = dst.salvar_modelo(previsor, MODELOS / "sobrevivencia_recorrencia.pkl", metadados={
            "tipo": "recorrencia_manutencao",
            "modelo": "andersen_gill + MCF",
            **previsor.metadados,
        })
        log.info("[recorrentes] modelo salvo: %s", caminho)
        comparacao.to_parquet(MODELOS / "recorrentes_coeficientes.parquet", index=False)
        erros_padrao.to_parquet(MODELOS / "recorrentes_erros_padrao.parquet", index=False)
        mcf.to_parquet(MODELOS / "recorrentes_mcf.parquet", index=False)
        frailty["por_usina"].to_parquet(MODELOS / "recorrentes_frailty_por_usina.parquet",
                                        index=False)
        previsoes.to_parquet(MODELOS / "recorrentes_esperadas_por_usina.parquet", index=False)

    return {"ajustes": ajustes, "comparacao": comparacao, "erros_padrao": erros_padrao,
            "mcf": mcf, "frailty": frailty, "previsor": previsor, "previsoes": previsoes}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    warnings.filterwarnings("ignore")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--horizontes", nargs="+", type=int, default=list(HORIZONTES_MESES))
    parser.add_argument("--sem-graficos", action="store_true")
    parser.add_argument("--sem-salvar", action="store_true")
    args = parser.parse_args()

    executar(tuple(args.horizontes), not args.sem_graficos, not args.sem_salvar)


if __name__ == "__main__":
    main()
