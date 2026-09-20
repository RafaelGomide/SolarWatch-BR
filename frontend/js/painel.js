/* Página inicial: KPIs nacionais, gráfico de geração e lista de usinas. */

import { buscar, escapar, etiquetaFonte, fmt, mostrarErro } from "./api.js";
import { barras, corDoTema, linha } from "./graficos.js";
import { iniciarTema } from "./tema.js";

const el = (id) => document.getElementById(id);
const estado = { usinas: [], cursor: null, filtros: { fonte: "", regiao: "" }, nacional: null };

/* --------------------------------- KPIs --------------------------------- */
async function carregarResumo() {
  // /health responde tanto na raiz quanto sob o prefixo da API
  const [nacional, saude] = await Promise.all([
    buscar("/geracao/nacional", { granularidade: "dia" }),
    buscar("/health").catch(() => null),
  ]);
  estado.nacional = nacional;

  const totais = { solar: 0, eolica: 0 };
  for (const ponto of nacional.data) totais[ponto.fonte] = (totais[ponto.fonte] || 0) + ponto.energia_mwh;

  const solar = fmt.energia(totais.solar);
  const eolica = fmt.energia(totais.eolica);
  const dias = new Set(nacional.data.map((p) => p.periodo)).size;

  el("kpis").innerHTML = `
    <article class="cartao kpi kpi--solar">
      <p class="kpi__rotulo">Geração solar</p>
      <p class="kpi__valor">${solar.valor}<span class="kpi__unidade">${solar.unidade}</span></p>
      <p class="kpi__nota">${dias} dias de histórico</p>
    </article>
    <article class="cartao kpi kpi--eolica">
      <p class="kpi__rotulo">Geração eólica</p>
      <p class="kpi__valor">${eolica.valor}<span class="kpi__unidade">${eolica.unidade}</span></p>
      <p class="kpi__nota">mesmo período</p>
    </article>
    <article class="cartao kpi kpi--ok">
      <p class="kpi__rotulo">Unidades monitoradas</p>
      <p class="kpi__valor">${fmt.inteiro(saude?.cobertura?.usinas)}</p>
      <p class="kpi__nota">usinas e conjuntos do ONS</p>
    </article>
    <article class="cartao kpi kpi--roxo">
      <p class="kpi__rotulo">Período coberto</p>
      <p class="kpi__valor" style="font-size:1.15rem">${fmt.data(nacional.inicio)} → ${fmt.data(nacional.fim)}</p>
      <p class="kpi__nota">atualizado pelo ETL, não em tempo real</p>
    </article>`;
}

/* ------------------------------- gráficos ------------------------------- */
function desenharNacional() {
  const dados = estado.nacional;
  if (!dados) return;
  const porFonte = (fonte) =>
    dados.data.filter((p) => p.fonte === fonte).map((p) => ({ x: new Date(p.periodo), y: p.energia_mwh }));

  linha(el("grafico-nacional"), [
    { nome: "Solar", cor: corDoTema("--cor-solar"), pontos: porFonte("solar") },
    { nome: "Eólica", cor: corDoTema("--cor-eolica"), pontos: porFonte("eolica") },
  ], {
    formatarY: (v) => `${Math.round(v / 1000)}k`,
    descricao: "Geração diária do SIN por fonte, em MWh",
  });
}

async function carregarPrevisao() {
  const destino = el("grafico-previsao");
  try {
    const [solar, eolica] = await Promise.all([
      buscar("/geracao/previsao", { fonte: "solar" }),
      buscar("/geracao/previsao", { fonte: "eolica" }),
    ]);
    const serie = (previsao, cor, nome) => ({
      nome, cor, tracejada: true,
      pontos: previsao.data.map((p) => ({ x: new Date(p.timestamp), y: p.energia_mwh_prevista })),
    });
    barras(destino, [
      serie(solar, corDoTema("--cor-solar"), "Solar prevista"),
      serie(eolica, corDoTema("--cor-eolica"), "Eólica prevista"),
    ], {
      formatarY: (v) => `${Math.round(v / 1000)}k`,
      formatarX: (x) => `${String(new Date(x).getHours()).padStart(2, "0")}h`,
      descricao: "Previsão horária das próximas 24 horas",
    });
    el("previsao-origem").textContent =
      `Modelo ${solar.modelo} · 24 h a partir de ${fmt.dataHora(solar.origem)} · ` +
      `RMSE solar ${fmt.inteiro(solar.metricas_backtesting?.rmse)} MWh`;
  } catch (erro) {
    mostrarErro(destino, erro);
  }
}

/* --------------------------------- lista --------------------------------- */
function linhaDaTabela(usina) {
  return `<tr>
    <td><a href="usina.html?id=${usina.usina_id}">${escapar(usina.nome)}</a></td>
    <td>${etiquetaFonte(usina.fonte)}</td>
    <td>${escapar(usina.regiao)}${usina.id_estado ? ` · ${escapar(usina.id_estado)}` : ""}</td>
    <td class="numero">${usina.potencia_mw == null
      ? '<span class="etiqueta etiqueta--neutra" title="Vínculo ONS×ANEEL incompleto">sem cadastro</span>'
      : fmt.potencia(usina.potencia_mw)}</td>
    <td>${fmt.data(usina.data_operacao)}</td>
  </tr>`;
}

async function carregarUsinas({ acrescentar = false } = {}) {
  const corpo = el("corpo-usinas");
  if (!acrescentar) corpo.innerHTML = `<tr><td colspan="5"><div class="carregando"></div></td></tr>`;
  try {
    const pagina = await buscar("/usinas", {
      limit: 25, cursor: acrescentar ? estado.cursor : null, ...estado.filtros,
    });
    estado.usinas = acrescentar ? [...estado.usinas, ...pagina.data] : pagina.data;
    estado.cursor = pagina.next_cursor;

    corpo.innerHTML = estado.usinas.length
      ? estado.usinas.map(linhaDaTabela).join("")
      : '<tr><td colspan="5" class="tabela__vazio">Nenhuma usina com esses filtros.</td></tr>';
    el("contagem-usinas").textContent = `${estado.usinas.length} de ${fmt.inteiro(pagina.total_estimado)}`;
    el("carregar-mais").classList.toggle("escondido", !pagina.next_cursor);
  } catch (erro) {
    corpo.innerHTML = `<tr><td colspan="5"></td></tr>`;
    mostrarErro(corpo.firstElementChild.firstElementChild, erro);
  }
}

/* --------------------------------- início -------------------------------- */
async function iniciar() {
  iniciarTema();
  document.addEventListener("tema-alterado", () => { desenharNacional(); carregarPrevisao(); });

  el("filtro-fonte").addEventListener("change", (e) => {
    estado.filtros.fonte = e.target.value;
    carregarUsinas();
  });
  el("filtro-regiao").addEventListener("change", (e) => {
    estado.filtros.regiao = e.target.value;
    carregarUsinas();
  });
  el("carregar-mais").addEventListener("click", () => carregarUsinas({ acrescentar: true }));

  try {
    await carregarResumo();
    desenharNacional();
  } catch (erro) {
    mostrarErro(el("kpis"), erro);
  }
  carregarPrevisao();
  carregarUsinas();
}

iniciar();
