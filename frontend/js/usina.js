/* Página de detalhe de uma usina: cadastro, geração, clima, previsão e sobrevivência. */

import { buscar, escapar, etiquetaFonte, fmt, mostrarErro } from "./api.js";
import { corDoTema, linha } from "./graficos.js";
import { iniciarTema } from "./tema.js";

const el = (id) => document.getElementById(id);
const idUsina = new URLSearchParams(location.search).get("id");
const estado = { geracao: null, clima: null };

/* ------------------------------- cadastro ------------------------------- */
function renderizarCabecalho(usina) {
  document.title = `${usina.nome} · SolarWatch BR`;
  el("nome-usina").textContent = usina.nome;
  el("etiquetas").innerHTML = `
    ${etiquetaFonte(usina.fonte)}
    <span class="etiqueta etiqueta--neutra">${escapar(usina.tipo_unidade)}</span>
    <span class="etiqueta etiqueta--neutra">${escapar(usina.regiao)}${usina.id_estado ? ` · ${escapar(usina.id_estado)}` : ""}</span>
    ${usina.qualidade_vinculo === "exata" || usina.qualidade_vinculo === "consistente"
      ? '<span class="etiqueta etiqueta--ok">cadastro confiável</span>'
      : `<span class="etiqueta etiqueta--risco" title="Vínculo ONS×ANEEL ${escapar(usina.qualidade_vinculo)}">cadastro parcial</span>`}`;

  el("cadastro").innerHTML = `
    <div><dt>Potência instalada</dt><dd>${fmt.potencia(usina.potencia_mw)}</dd></div>
    <div><dt>Município</dt><dd>${escapar(usina.municipio ?? "—")}</dd></div>
    <div><dt>Em operação desde</dt><dd>${fmt.data(usina.data_operacao)}</dd></div>
    <div><dt>Usinas da ANEEL vinculadas</dt><dd>${fmt.inteiro(usina.n_usinas_aneel)}</dd></div>
    <div><dt>Pico horário observado</dt><dd>${fmt.decimal(usina.pico_geracao_mw)} MWh</dd></div>
    <div><dt>Coordenadas</dt><dd>${usina.lat == null ? "—" : `${usina.lat.toFixed(3)}, ${usina.lon.toFixed(3)}`}</dd></div>`;

  if (usina.potencia_mw == null) {
    el("aviso-cadastro").classList.remove("escondido");
  }
}

/* -------------------------------- geração -------------------------------- */
function desenharGeracao() {
  if (!estado.geracao) return;
  const pontos = estado.geracao.data.map((p) => ({ x: new Date(p.timestamp), y: p.energia_mwh }));
  const cor = corDoTema(estado.geracao.fonte === "solar" ? "--cor-solar" : "--cor-eolica");
  linha(el("grafico-geracao"), [{ nome: "Geração horária", cor, pontos }], {
    formatarY: (v) => fmt.inteiro(v),
    formatarX: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }),
    descricao: "Geração horária da usina no período",
  });
}

async function carregarGeracao(dias) {
  const destino = el("grafico-geracao");
  destino.innerHTML = '<div class="carregando" style="height:200px"></div>';
  try {
    const fim = new Date(estado.ultimaMedicao ?? Date.now());
    const inicio = new Date(fim);
    inicio.setDate(inicio.getDate() - dias);
    const iso = (d) => d.toISOString().slice(0, 10);

    estado.geracao = await buscar(`/usinas/${idUsina}/geracao`, { inicio: iso(inicio), fim: iso(fim) });
    desenharGeracao();

    const total = fmt.energia(estado.geracao.total_mwh);
    const faltantes = estado.geracao.data.filter((p) => p.flag_qualidade === "faltante").length;
    el("resumo-geracao").innerHTML =
      `${total.valor} ${total.unidade} em ${fmt.inteiro(estado.geracao.horas)} horas` +
      (faltantes ? ` · <span style="color:var(--cor-aviso)">${faltantes} h sem medição</span>` : "");
  } catch (erro) {
    mostrarErro(destino, erro);
  }
}

/* --------------------------------- clima --------------------------------- */
async function carregarClima() {
  const destino = el("grafico-clima");
  try {
    estado.clima = await buscar(`/usinas/${idUsina}/clima`);
    const serie = (campo, cor, nome) => ({
      nome, cor, pontos: estado.clima.data.map((p) => ({ x: new Date(p.data), y: p[campo] })),
    });
    const ehSolar = estado.geracao?.fonte === "solar";
    linha(destino, [
      ehSolar
        ? serie("irradiancia_kwh_m2", corDoTema("--roxo-400"), "Irradiância (kWh/m²/dia)")
        : serie("vento_ms", corDoTema("--azul-400"), "Vento a 50 m (m/s)"),
      serie("temperatura_c", corDoTema("--verde-400"), "Temperatura (°C)"),
    ], { formatarY: (v) => v.toFixed(1), area: false, descricao: "Clima diário do ponto de referência" });

    el("origem-clima").innerHTML =
      `Ponto <code>${escapar(estado.clima.local_clima)}</code> · ` +
      `${estado.clima.distancia_km == null ? "mesma UF" : `${fmt.inteiro(estado.clima.distancia_km)} km da usina`} · ` +
      `<span title="${escapar(estado.clima.aviso)}">vínculo ${escapar(estado.clima.metodo_vinculo_clima)}</span>`;
  } catch (erro) {
    if (erro.status === 404) {
      destino.innerHTML = '<p class="tabela__vazio">Sem ponto de clima de referência para esta unidade.</p>';
    } else {
      mostrarErro(destino, erro);
    }
  }
}

/* -------------------------------- previsão -------------------------------- */
async function carregarPrevisao() {
  const destino = el("grafico-previsao");
  try {
    const previsao = await buscar(`/usinas/${idUsina}/previsao`);
    const cor = corDoTema(previsao.fonte === "solar" ? "--cor-solar" : "--cor-eolica");
    linha(destino, [{
      nome: "Previsão (24 h)", cor, tracejada: true,
      pontos: previsao.data.map((p) => ({ x: new Date(p.timestamp), y: p.energia_mwh_prevista })),
    }], {
      formatarY: (v) => fmt.inteiro(v),
      formatarX: (x) => `${String(new Date(x).getHours()).padStart(2, "0")}h`,
      altura: 200, descricao: "Previsão de geração das próximas 24 horas",
    });

    el("nota-previsao").innerHTML =
      `Modelo <strong>${escapar(previsao.modelo)}</strong> treinado na geração agregada de ${fmt.fonte(previsao.fonte)}; ` +
      `o valor por usina é um <strong>rateio</strong> de ${fmt.percentual(previsao.participacao_usina, 2)} ` +
      `da previsão da fonte. Janela a partir de ${fmt.dataHora(previsao.origem)}. ${escapar(previsao.premissa_clima)}`;
  } catch (erro) {
    if (erro.status === 503) {
      destino.innerHTML = '<p class="tabela__vazio">Modelo de previsão indisponível no momento.</p>';
    } else {
      mostrarErro(destino, erro);
    }
  }
}

/* ------------------------------ sobrevivência ------------------------------ */
function corDaProbabilidade(p) {
  if (p >= 0.75) return "var(--verde-500)";
  if (p >= 0.5) return "var(--azul-500)";
  if (p >= 0.3) return "var(--roxo-500)";
  return "var(--vermelho-500)";
}

async function carregarSobrevivencia() {
  const destino = el("sobrevivencia");
  try {
    const dados = await buscar(`/usinas/${idUsina}/sobrevivencia`);
    destino.innerHTML = `
      <div class="aviso" role="note">
        <span aria-hidden="true">⚠</span>
        <span><strong>Dado simulado.</strong> ${escapar(dados.aviso)}</span>
      </div>
      <dl class="lista-definicoes" style="margin:1rem 0">
        <div><dt>Idade da usina</dt><dd>${fmt.decimal(dados.idade_anos)} anos</dd></div>
        <div><dt>Risco relativo</dt><dd>${fmt.decimal(dados.risco_relativo)}×</dd></div>
        <div><dt>Tempo mediano previsto</dt><dd>${fmt.decimal(dados.tempo_mediano_anos)} anos</dd></div>
      </dl>
      ${dados.horizontes.map((h) => `
        <div class="horizonte">
          <span class="horizonte__rotulo">${h.horizonte_meses} meses</span>
          <div class="barra"><div class="barra__preenchimento"
               style="width:${(h.probabilidade_sobrevivencia * 100).toFixed(1)}%;--barra-cor:${corDaProbabilidade(h.probabilidade_sobrevivencia)}"></div></div>
          <span class="horizonte__valor">${fmt.percentual(h.probabilidade_sobrevivencia)}</span>
        </div>`).join("")}
      <p style="font-size:0.82rem;margin-top:0.9rem">
        Probabilidade de seguir <strong>sem manutenção corretiva</strong>, ${dados.condicional_na_idade
          ? "dado que a usina já operou esse tempo sem evento" : "a partir da entrada em operação"}.
        Estimativa por <code>${escapar(dados.metodo_extrapolacao)}</code>.</p>`;
  } catch (erro) {
    if (erro.status === 503 || erro.status === 404) {
      destino.innerHTML = `<p class="tabela__vazio">${escapar(erro.message)}</p>`;
    } else {
      mostrarErro(destino, erro);
    }
  }
}

/* --------------------------------- início -------------------------------- */
async function iniciar() {
  iniciarTema();
  if (!idUsina) {
    document.querySelector("main .envoltorio").innerHTML =
      '<div class="erro">Informe a usina: <code>usina.html?id=12</code></div>';
    return;
  }

  el("periodo-geracao").addEventListener("change", (e) => carregarGeracao(Number(e.target.value)));
  document.addEventListener("tema-alterado", () => { desenharGeracao(); carregarClima(); carregarPrevisao(); });

  try {
    const usina = await buscar(`/usinas/${idUsina}`);
    estado.ultimaMedicao = usina.ultima_medicao_utc;
    renderizarCabecalho(usina);
  } catch (erro) {
    mostrarErro(el("cabecalho-usina"), erro);
    return;
  }

  await carregarGeracao(Number(el("periodo-geracao").value));
  carregarClima();
  carregarPrevisao();
  carregarSobrevivencia();
}

iniciar();
