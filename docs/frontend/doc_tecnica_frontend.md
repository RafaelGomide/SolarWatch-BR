# Documentação Técnica — Frontend

> Escopo: tudo sobre `frontend/` — o painel estático do SolarWatch BR. Cobre o design system (paleta, tema escuro padrão), a estrutura das três páginas, o cliente da API, os gráficos em SVG puro, acessibilidade, o que foi verificado no navegador e as limitações.
>
> Documentos relacionados: [backend](../backend/doc_tecnica_backend.md) (a API consumida), [banco](../DB/doc_tecnica_db.md), [séries temporais](../ML/doc_tecnica_series_temporais.md) e [sobrevivência](../ML/doc_tecnica_analise_sobrevivencia.md) (os modelos exibidos).

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Estrutura da pasta](#2-estrutura-da-pasta)
3. [Como executar](#3-como-executar)
4. [Design system](#4-design-system)
5. [Tema escuro por padrão](#5-tema-escuro-por-padrão)
6. [As páginas](#6-as-páginas)
7. [Cliente da API (`api.js`)](#7-cliente-da-api-apijs)
8. [Gráficos em SVG puro (`graficos.js`)](#8-gráficos-em-svg-puro-graficosjs)
9. [Honestidade na interface](#9-honestidade-na-interface)
10. [Estados de carregamento e erro](#10-estados-de-carregamento-e-erro)
11. [Acessibilidade](#11-acessibilidade)
12. [Verificação no navegador](#12-verificação-no-navegador)
13. [Decisões de design](#13-decisões-de-design)
14. [Limitações e próximos passos](#14-limitações-e-próximos-passos)

---

## 1. Visão geral

Painel estático em **HTML + CSS + JavaScript puro**, sem framework, sem build e sem nenhuma dependência de terceiros. É servido pelo próprio backend FastAPI em `/app` (system design §6.2: um processo só, um free tier só).

| Característica | Valor |
|---|---|
| Páginas | 3 (painel, usina, metodologia) |
| Dependências | **nenhuma** — nem biblioteca de gráficos |
| Build | nenhum; os arquivos são servidos como estão |
| Tema padrão | **escuro**, com alternância persistida |
| CSS | 327 linhas, arquivo único |
| JavaScript | ~600 linhas em 5 módulos ES |
| Gráficos | SVG gerado em tempo de execução |

Por que sem framework: o painel tem 3 páginas e consome 6 endpoints de leitura. React ou Vue trariam build, bundle e dependências para um problema que `fetch` + template string resolvem. A regra do projeto é a mesma do system design: complexidade precisa se pagar.

---

## 2. Estrutura da pasta

```
frontend/
├── index.html          # painel: KPIs, geração nacional, previsão, lista de usinas
├── usina.html          # detalhe de uma unidade
├── metodologia.html    # fontes, tratamento, modelos e ressalvas
├── css/
│   └── estilo.css      # design system inteiro: tokens, componentes, responsivo
└── js/
    ├── api.js          # cliente da API + formatação pt-BR
    ├── graficos.js     # linhas e barras em SVG
    ├── tema.js         # alternância de tema
    ├── painel.js       # lógica do index.html
    └── usina.js        # lógica do usina.html
```

Cada página carrega **um** módulo de entrada (`type="module"`), que importa o que precisa. Sem bundler: os imports ES nativos resolvem direto no navegador.

---

## 3. Como executar

```bash
uvicorn backend.main:app --reload
# painel:       http://localhost:8000/app/
# uma usina:    http://localhost:8000/app/usina.html?id=12
# metodologia:  http://localhost:8000/app/metodologia.html
```

O backend monta `frontend/` em `/app` **se a pasta existir** (`main.py`), então não há passo extra de deploy: o mesmo processo serve API e interface.

Aberto direto do disco (`file://`), o `api.js` aponta para `http://localhost:8000/api/v1` — útil para editar CSS sem subir nada, embora o CORS precise liberar a origem.

---

## 4. Design system

Tudo em `css/estilo.css`, via variáveis CSS.

### 4.1 A paleta

Quatro matizes, cinco tonalidades cada (300, 400, 500, 700, 900):

| Matiz | 300 | 400 | 500 | 700 | 900 |
|---|---|---|---|---|---|
| **Azul** | `#7dd3fc` | `#38bdf8` | `#0ea5e9` | `#0369a1` | `#0c3049` |
| **Vermelho** | `#fca5a5` | `#f87171` | `#ef4444` | `#b91c1c` | `#4c1111` |
| **Verde** | `#86efac` | `#4ade80` | `#22c55e` | `#15803d` | `#10331f` |
| **Roxo** | `#d8b4fe` | `#c084fc` | `#a855f7` | `#7e22ce` | `#34134f` |

### 4.2 Papéis semânticos

Cor solta vira decoração; cor com papel vira informação. Cada matiz tem uma função fixa em todo o painel:

| Papel | Cor | Onde aparece |
|---|---|---|
| **Solar** | roxo | série solar nos gráficos, etiqueta da fonte, KPI |
| **Eólica** | azul | série eólica, etiqueta, KPI |
| **Positivo** | verde | "cadastro confiável", alta probabilidade de sobrevivência, KPI de cobertura |
| **Risco / aviso** | vermelho | banner de dado simulado, "cadastro parcial", horas sem medição, baixa probabilidade |

Solar costuma ser amarelo em painéis de energia, mas o amarelo está fora da paleta pedida. Roxo foi escolhido porque, contra o azul da eólica, tem contraste de matiz alto e continua distinguível para as formas mais comuns de daltonismo (as duas séries também diferem em posição e legenda, não só em cor).

As tonalidades **900** entram como fundo dos gradientes do `body`; as **300/400** carregam texto e linhas no tema escuro; as **700** assumem no tema claro, onde precisam de mais contraste contra branco.

### 4.3 Tokens de superfície e métrica

```css
--fundo, --fundo-elevado, --superficie, --superficie-hover   /* camadas */
--borda, --borda-forte                                        /* separadores */
--texto, --texto-suave, --texto-fraco                         /* hierarquia tipográfica */
--raio, --raio-pequeno, --espaco, --largura-maxima            /* métrica */
--transicao                                                    /* 160ms ease */
```

Nenhum valor de cor aparece solto no CSS de componente: tudo passa por token, que é o que permite trocar o tema inteiro redefinindo um bloco.

### 4.4 Componentes

`.cartao`, `.kpi`, `.etiqueta` (solar/eólica/ok/risco/neutra), `.aviso` (risco e neutro), `.botao`, `.campo`, `.tabela-envoltorio`, `.barra` + `.horizonte`, `.carregando`, `.erro`, `.lista-definicoes`, `.legenda`, `.grade` (KPIs e duas colunas).

O KPI usa uma faixa colorida de 3px na base (`::after`) em vez de fundo colorido: identifica a categoria sem competir com o número, que é o que importa.

---

## 5. Tema escuro por padrão

```html
<html lang="pt-BR" data-tema="escuro">
```

O atributo já vem no HTML, **antes de qualquer JavaScript** — isso evita o flash branco que acontece quando o tema é aplicado só depois do primeiro paint.

```css
:root { /* tokens do tema escuro */ }
:root[data-tema="claro"] { /* redefine os mesmos tokens */ }
```

O `tema.js` lê a preferência salva no `localStorage` (dentro de `try/catch`, porque em janela privada o acesso pode lançar) e, na ausência dela, **mantém o escuro**. A alternância dispara um evento `tema-alterado`, que as páginas escutam para **redesenhar os gráficos** com as cores do tema novo — sem isso, as linhas ficariam com a cor antiga, já que a cor é lida do CSS no momento do desenho.

`<meta name="color-scheme" content="dark light">` faz os controles nativos (barras de rolagem, `<select>`) acompanharem o tema.

---

## 6. As páginas

### 6.1 Painel (`index.html` + `painel.js`)

| Bloco | Conteúdo | Endpoint |
|---|---|---|
| KPIs | Geração solar (24 TWh) e eólica (28,3 TWh) no período, unidades monitoradas (308), período coberto | `/geracao/nacional`, `/health` |
| Geração diária do SIN | Gráfico de linhas com área, duas séries | `/geracao/nacional?granularidade=dia` |
| Previsão de 24 h | Gráfico de barras agrupadas, com a origem e o RMSE do modelo no subtítulo | `/geracao/previsao` (solar e eólica) |
| Usinas monitoradas | Tabela com filtros de fonte e subsistema, botão "carregar mais" | `/usinas` |

A paginação usa o **cursor** da API: o botão "carregar mais" envia `next_cursor` e concatena os resultados; ele some quando a API para de devolver cursor.

### 6.2 Usina (`usina.html` + `usina.js`)

Recebe `?id=` na URL. Sem id, mostra uma mensagem explicando o formato esperado.

| Bloco | Conteúdo | Endpoint |
|---|---|---|
| Cabeçalho | Nome, etiquetas (fonte, tipo, local, confiabilidade do cadastro) | `/usinas/{id}` |
| Cadastro | Potência, município, data de operação, usinas ANEEL vinculadas, pico observado, coordenadas | idem |
| Geração medida | Gráfico horário com seletor de 7/15/30/60 dias e resumo (total + horas sem medição) | `/usinas/{id}/geracao` |
| Clima | Irradiância (solar) **ou** vento a 50 m (eólica), mais temperatura | `/usinas/{id}/clima` |
| Previsão de 24 h | Linha tracejada + nota explicando o rateio | `/usinas/{id}/previsao` |
| Sobrevivência | Banner de dado simulado, idade/risco/tempo mediano e 4 barras de probabilidade | `/usinas/{id}/sobrevivencia` |

O gráfico de clima **muda conforme a fonte**: irradiância é a variável que explica geração solar; vento a 50 m é a que explica eólica. Mostrar as duas sempre seria ruído.

A janela de geração é ancorada na **última medição da usina** (`ultima_medicao_utc`), não em "hoje": o banco é estático e "últimos 15 dias a partir de hoje" devolveria vazio quando o ETL estivesse atrasado.

### 6.3 Metodologia (`metodologia.html`)

Página só de conteúdo: as quatro fontes em cartões coloridos pelo papel de cada uma, o critério do vínculo ONS × ANEEL (com a contagem por qualidade), as cinco regras de tratamento de dados, os dois modelos com suas métricas e a nota de que o banco é estático.

Ela existe porque o painel mostra números que **precisam de contexto** — sobretudo o "sem cadastro" e a probabilidade de sobrevivência simulada. Cada aviso no painel aponta para cá.

---

## 7. Cliente da API (`api.js`)

```js
export const BASE_API = location.protocol === "file:"
  ? "http://localhost:8000/api/v1"
  : `${location.origin}/api/v1`;
```

Servido pelo backend, usa a mesma origem (sem CORS); aberto do disco, cai no localhost.

**Tratamento de erro alinhado ao backend:** a API responde Problem Details (RFC 9457), então o cliente lê `title` e `detail` do corpo e os expõe em uma `ErroApi` com `status`. É isso que permite as páginas distinguirem os casos:

| Status | Tratamento na interface |
|---|---|
| 0 (rede) | "Não foi possível falar com a API. Ela está rodando?" |
| 404 | Mensagem específica do bloco (ex.: "sem ponto de clima de referência") |
| 503 | "Modelo indisponível no momento" — só naquele card |
| outros | Título + detalhe vindos da API |

**Formatação pt-BR** (`fmt`): `Intl.NumberFormat` para números e datas, escala automática de energia (MWh → GWh → TWh) e de potência (MW → GW), percentual e nomes de fonte. Assim nenhuma página repete regra de formatação.

**`escapar()`**: todo texto vindo da API passa por escape antes de entrar em `innerHTML`. Os nomes de usina vêm de fonte pública e não deveriam conter HTML, mas isso não é garantia — a defesa fica no ponto de injeção.

---

## 8. Gráficos em SVG puro (`graficos.js`)

Duas funções, mesma assinatura: `linha(elemento, series, opcoes)` e `barras(...)`, onde cada série é `{nome, cor, pontos: [{x, y}], tracejada?}`.

O módulo cuida de:

- **escalas** lineares de x e y, com margem para eixos e 8% de folga no topo;
- **eixos e grade**: 5 linhas horizontais com rótulos formatáveis, rótulos de tempo espaçados automaticamente;
- **área sob a curva** com gradiente que vai de 35% de opacidade até transparente;
- **linha tracejada** para previsões, o que as distingue do observado sem depender só de cor;
- **legenda** gerada a partir das séries;
- **valores nulos**: pontos com `y == null` (as horas `faltante` do ONS) são descartados da linha, o que deixa a lacuna visível em vez de fingir continuidade.

`corDoTema("--cor-solar")` lê a cor do design system em tempo de desenho, e é o que faz os gráficos acompanharem a troca de tema.

Por que não Chart.js ou D3: seriam de 70 KB a 250 KB para dois tipos de gráfico sem interação complexa. A implementação inteira tem 155 linhas e nenhum download.

---

## 9. Honestidade na interface

O projeto tem ressalvas reais, e a interface as mostra em vez de escondê-las atrás de números bonitos:

| Ressalva | Como aparece |
|---|---|
| Eventos de manutenção são simulados | Banner vermelho no topo do card de sobrevivência, aviso no rodapé de todas as páginas e seção na metodologia |
| 212 das 308 unidades sem potência confiável | Etiqueta "sem cadastro" na tabela e "cadastro parcial" na página da usina, com link para o critério |
| Previsão por usina é um rateio | Nota abaixo do gráfico, com a participação exata (ex.: "rateio de 0,55% da previsão da fonte") |
| Clima é do ponto mais próximo, não da usina | "Ponto `caetite_ba` · 7 km da usina · vínculo mais_proximo" |
| Clima do horizonte é suposto conhecido | Frase da API repetida na nota da previsão |
| Horas sem medição | Contagem em vermelho no resumo ("N h sem medição") e lacuna no gráfico |
| Dado não é tempo real | KPI "período coberto" + "atualizado pelo ETL, não em tempo real" |

Esses textos **vêm da API** (campos `aviso`, `premissa_clima`, `metodo`, `qualidade_vinculo`), não são strings fixas no frontend. Se a ressalva mudar no backend, a tela acompanha.

---

## 10. Estados de carregamento e erro

Cada bloco tem os três estados, sem página em branco:

- **Carregando:** `.carregando` — um retângulo com brilho deslizante, dimensionado para o conteúdo que virá (evita o pulo de layout).
- **Vazio:** mensagem explicativa no lugar do gráfico/tabela ("Sem dados no período", "Nenhuma usina com esses filtros").
- **Erro:** `.erro` com `role="alert"`, mostrando título e detalhe do Problem Details.

Falhas são **isoladas por bloco**: se `/previsao` responder 503, o card da previsão mostra a mensagem e o restante da página continua carregando normalmente — o mesmo princípio de degradação graciosa do backend (§10.5 do system design), agora na interface.

---

## 11. Acessibilidade

| Item | Implementação |
|---|---|
| Idioma | `<html lang="pt-BR">` |
| Navegação | `<nav aria-label="Principal">` e `aria-current="page"` na página ativa |
| Gráficos | `role="img"` + `aria-label` descrevendo o conteúdo do gráfico |
| Tabela | `<caption>` (visualmente oculto), `<th scope="col">` |
| Avisos | `role="note"`; erros com `role="alert"` |
| Botão de tema | `aria-pressed` e `aria-label` que mudam com o estado |
| Foco | `:focus-visible` com contorno de 2px na cor azul |
| Movimento | `@media (prefers-reduced-motion: reduce)` zera animações e transições |
| Cor | Nunca é o único canal: fonte tem etiqueta com texto, séries têm legenda, previsão é tracejada |
| Responsivo | Layout em `auto-fit`/`minmax`; abaixo de 620px o cabeçalho quebra e a navegação rola |

---

## 12. Verificação no navegador

O painel foi carregado no navegador contra o backend real (porta 8088) e verificado ponto a ponto:

| Verificação | Resultado |
|---|---|
| Painel carrega KPIs | 24 TWh solar · 28,3 TWh eólica · 308 unidades · 01/07 → 17/09/2026 ✅ |
| Gráfico de geração nacional | 158 pontos, duas séries, eixos com rótulos ✅ |
| Previsão 24 h | Barras solar/eólica, subtítulo com modelo e RMSE ✅ |
| Tabela e filtros | 25 de 308, filtros e "carregar mais" ✅ |
| Página da usina (id=12) | Cadastro completo, 32,6 GWh em 360 horas, clima a 7 km, previsão com rateio de 0,55% ✅ |
| Card de sobrevivência | Banner de simulado + barras 63% / 40% / 15% / 5% com cores por faixa ✅ |
| Alternância de tema | Claro e escuro, com gráficos redesenhados ✅ |
| Console | Nenhum erro ✅ |

---

## 13. Decisões de design

1. **Sem framework e sem build.** Três páginas, seis endpoints de leitura. Um bundler aqui custaria mais do que entrega.
2. **Tema escuro no HTML, não no JS.** Elimina o flash branco no primeiro paint.
3. **Cor com papel fixo.** Roxo é sempre solar, azul sempre eólica, verde sempre positivo, vermelho sempre risco — em gráfico, etiqueta, KPI e barra.
4. **Gráficos próprios em SVG.** 155 linhas contra dezenas de KB de biblioteca, e controle total sobre o tratamento de nulos e a troca de tema.
5. **Janela de dados ancorada na última medição**, não em `Date.now()` — o banco é estático.
6. **Ressalvas vindas da API.** Os avisos são campos do payload; a interface os exibe em vez de reescrevê-los.
7. **Estado por bloco.** Cada card carrega, falha e se recupera sozinho.

---

## 14. Limitações e próximos passos

| # | Limitação | Impacto | Próximo passo |
|---|---|---|---|
| 1 | Sem mapa | Lat/lon aparecem como texto, embora 162 unidades tenham coordenada | Mapa com SVG do Brasil ou Leaflet (a decidir: Leaflet traria a primeira dependência) |
| 2 | Gráficos sem tooltip | Não dá para ler o valor exato de um ponto | Camada de hover com `<rect>` transparentes por ponto e um balão posicionado |
| 3 | Sem estado na URL da lista | Filtros não são compartilháveis nem sobrevivem ao recarregar | Sincronizar `fonte`/`regiao` com `URLSearchParams` |
| 4 | Sem comparação entre usinas | Só dá para ver uma por vez | Página de comparação com séries sobrepostas |
| 5 | Sem testes automatizados | Regressões visuais e de lógica passam despercebidas | Playwright para um smoke por página + verificação de contraste |
| 6 | Sem `Cache-Control` nos estáticos | Recarrega tudo a cada visita | Configurar cabeçalhos no `StaticFiles` do backend |
| 7 | Traduções fixas em português | Sem i18n | Só se houver público para isso; hoje seria complexidade sem retorno |
| 8 | Sem indicação de "dado velho" | Se o ETL parar, o painel mostra dado antigo sem alarde | Destacar o KPI de período quando o fim for anterior a N dias |

---

*Documento gerado em 20/09/2026 a partir do código de `frontend/` e da verificação no navegador contra o backend real.*
