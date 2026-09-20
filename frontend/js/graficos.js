/* Gráficos em SVG puro — sem biblioteca externa.
   Todos recebem uma lista de séries {nome, cor, pontos:[{x: Date|number, y}]}. */

const NS = "http://www.w3.org/2000/svg";
const MARGEM = { topo: 14, direita: 12, baixo: 26, esquerda: 52 };

const criar = (tag, atributos = {}) => {
  const elemento = document.createElementNS(NS, tag);
  for (const [chave, valor] of Object.entries(atributos)) elemento.setAttribute(chave, valor);
  return elemento;
};

function escalas(series, largura, altura) {
  const xs = series.flatMap((s) => s.pontos.map((p) => +p.x));
  const ys = series.flatMap((s) => s.pontos.map((p) => p.y)).filter((y) => y != null);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMax = Math.max(...ys, 0) * 1.08 || 1;
  const larguraUtil = largura - MARGEM.esquerda - MARGEM.direita;
  const alturaUtil = altura - MARGEM.topo - MARGEM.baixo;
  return {
    x: (v) => MARGEM.esquerda + ((+v - xMin) / (xMax - xMin || 1)) * larguraUtil,
    y: (v) => MARGEM.topo + alturaUtil - (v / yMax) * alturaUtil,
    yMax, alturaUtil, larguraUtil,
  };
}

function eixos(svg, escala, largura, altura, rotulosX, formatarY) {
  // linhas de grade horizontais + rótulos do eixo Y
  for (let i = 0; i <= 4; i++) {
    const valor = (escala.yMax / 4) * i;
    const y = escala.y(valor);
    svg.appendChild(criar("line", {
      class: "grade-linha", x1: MARGEM.esquerda, x2: largura - MARGEM.direita, y1: y, y2: y,
    }));
    const rotulo = criar("text", { x: MARGEM.esquerda - 8, y: y + 3, "text-anchor": "end" });
    rotulo.textContent = formatarY(valor);
    svg.appendChild(rotulo);
  }
  svg.appendChild(criar("line", {
    class: "eixo", x1: MARGEM.esquerda, x2: largura - MARGEM.direita,
    y1: altura - MARGEM.baixo, y2: altura - MARGEM.baixo,
  }));
  rotulosX.forEach(({ x, texto }) => {
    const rotulo = criar("text", { x: escala.x(x), y: altura - MARGEM.baixo + 15, "text-anchor": "middle" });
    rotulo.textContent = texto;
    svg.appendChild(rotulo);
  });
}

function rotulosDeTempo(pontos, quantidade = 5, formatar) {
  if (!pontos.length) return [];
  const passo = Math.max(1, Math.floor(pontos.length / (quantidade - 1)));
  const escolhidos = pontos.filter((_, i) => i % passo === 0);
  return escolhidos.map((p) => ({ x: +p.x, texto: formatar(p.x) }));
}

/**
 * Gráfico de linhas (opcionalmente com área sob a curva).
 * `series`: [{nome, cor, pontos, tracejada?}]
 */
export function linha(elemento, series, opcoes = {}) {
  const { largura = 760, altura = 260, formatarY = (v) => Math.round(v), formatarX, area = true } = opcoes;
  elemento.innerHTML = "";
  const comDados = series.filter((s) => s.pontos.some((p) => p.y != null));
  if (!comDados.length) {
    elemento.innerHTML = '<p class="tabela__vazio">Sem dados no período.</p>';
    return;
  }

  const svg = criar("svg", {
    class: "grafico", viewBox: `0 0 ${largura} ${altura}`, role: "img",
    "aria-label": opcoes.descricao || "Gráfico de linhas",
  });
  const escala = escalas(comDados, largura, altura);
  const formatoX = formatarX || ((x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }));
  eixos(svg, escala, largura, altura, rotulosDeTempo(comDados[0].pontos, 5, formatoX), formatarY);

  comDados.forEach((serie, indice) => {
    const validos = serie.pontos.filter((p) => p.y != null);
    const caminho = validos.map((p, i) => `${i ? "L" : "M"}${escala.x(p.x).toFixed(1)},${escala.y(p.y).toFixed(1)}`).join(" ");

    if (area && !serie.tracejada) {
      const base = altura - MARGEM.baixo;
      const gradiente = criar("linearGradient", { id: `grad-${indice}-${Math.random().toString(36).slice(2, 7)}`, x1: 0, y1: 0, x2: 0, y2: 1 });
      gradiente.appendChild(criar("stop", { offset: "0%", "stop-color": serie.cor, "stop-opacity": 0.35 }));
      gradiente.appendChild(criar("stop", { offset: "100%", "stop-color": serie.cor, "stop-opacity": 0 }));
      svg.appendChild(gradiente);
      svg.appendChild(criar("path", {
        d: `${caminho} L${escala.x(validos.at(-1).x).toFixed(1)},${base} L${escala.x(validos[0].x).toFixed(1)},${base} Z`,
        fill: `url(#${gradiente.id})`, stroke: "none",
      }));
    }

    svg.appendChild(criar("path", {
      d: caminho, fill: "none", stroke: serie.cor, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
      ...(serie.tracejada ? { "stroke-dasharray": "5 4" } : {}),
    }));
  });

  elemento.appendChild(svg);
  if (opcoes.legenda !== false) elemento.appendChild(legenda(comDados));
}

/** Gráfico de barras verticais agrupadas por categoria. */
export function barras(elemento, series, opcoes = {}) {
  const { largura = 760, altura = 260, formatarY = (v) => Math.round(v), formatarX } = opcoes;
  elemento.innerHTML = "";
  if (!series.length || !series[0].pontos.length) {
    elemento.innerHTML = '<p class="tabela__vazio">Sem dados no período.</p>';
    return;
  }

  const svg = criar("svg", {
    class: "grafico", viewBox: `0 0 ${largura} ${altura}`, role: "img",
    "aria-label": opcoes.descricao || "Gráfico de barras",
  });
  const escala = escalas(series, largura, altura);
  const formatoX = formatarX || ((x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }));
  eixos(svg, escala, largura, altura, rotulosDeTempo(series[0].pontos, 5, formatoX), formatarY);

  const total = series[0].pontos.length;
  const larguraGrupo = escala.larguraUtil / total;
  const larguraBarra = Math.max(1.5, (larguraGrupo * 0.72) / series.length);
  const base = altura - MARGEM.baixo;

  series.forEach((serie, indiceSerie) => {
    serie.pontos.forEach((ponto) => {
      if (ponto.y == null) return;
      const x = escala.x(ponto.x) - (larguraBarra * series.length) / 2 + indiceSerie * larguraBarra;
      const y = escala.y(ponto.y);
      svg.appendChild(criar("rect", {
        x: x.toFixed(1), y: y.toFixed(1), width: larguraBarra.toFixed(1),
        height: Math.max(0, base - y).toFixed(1), fill: serie.cor, rx: 1.5, opacity: 0.9,
      }));
    });
  });

  elemento.appendChild(svg);
  elemento.appendChild(legenda(series));
}

function legenda(series) {
  const div = document.createElement("div");
  div.className = "legenda";
  div.innerHTML = series
    .map((s) => `<span style="color:${s.cor}"><i></i>${s.nome}</span>`)
    .join("");
  return div;
}

/** Lê uma cor do design system (ex.: "--cor-solar"). */
export function corDoTema(variavel) {
  return getComputedStyle(document.documentElement).getPropertyValue(variavel).trim();
}
