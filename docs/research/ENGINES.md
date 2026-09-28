# Quill: motores de transcrição e custo mensal (Fase 1)

**Estado: a aguardar a decisão do Sponsor.** Nenhum serviço pago foi adotado e não foi escrito código de produto. Medições feitas a 2026-09-28.

Este documento compara os motores de transcrição candidatos na voz real do Sponsor e prepara a decisão de motor e de custo mensal. Contém só números agregados: nenhum texto falado, nenhum nome de projeto real e nenhum caminho da máquina. Os resultados por frase (referência, hipótese, veredicto de intenção) ficam apenas em `bench/results/`, que o Git ignora. Os números vêm de [engines-summary.json](engines-summary.json); o harness está em [bench/](../../bench/README.md).

## Resumo

- **Melhor opção a 0 EUR:** Whisper large-v3 local com vocabulário (`hints`): WER 28,3 %, erro em termos ingleses 23,5 %, erro em nomes de projeto 21,1 %, intenção preservada 52,3 %, latência p95 1,30 s. É também a melhor opção medida em qualidade, incluindo as cloud.
- **Melhor opção cloud (gratuita hoje, paga a partir do limite):** Groq `whisper-large-v3-turbo` com vocabulário: WER 30,8 %, termos 58,8 %, nomes 23,7 %, intenção 38,6 %, p95 0,49 s; no plano pago custaria 0,60 a 3,00 USD por mês para 15 h.
- **Nenhum motor chega às metas por omissão da Fase 2** (WER ≤ 10 %, intenção ≥ 95 %). O vocabulário é a alavanca que mais ajuda (o erro em termos do large-v3 local desce de 70,6 % para 23,5 %); a limpeza com `qwen3:8b` quase não muda o WER e soma 0,6 a 1,3 s de latência.
- Gemini não foi medido nos 44 takes (plano gratuito limitado a 25 pedidos por dia) e Deepgram não foi medido por decisão do Sponsor.
- **Recomendação:** Whisper large-v3 local com vocabulário, 0 EUR/mês. Ver [Decisão](#decisão-a-aguardar-o-sponsor).

## Método

**Dados.** 44 gravações reais e válidas da voz do Sponsor (`pt-NN.wav`, 16 kHz mono, microfone), lidas no sítio e nunca copiadas para este repositório. Os takes descartados (`*.invalida-*`) ficam de fora. No total são 104,0 s de áudio (0,64 a 6,27 s por take, mediana 2,18 s), 360 palavras de referência, 17 ocorrências de termos ingleses/técnicos e 38 ocorrências de nomes de projeto em 31 takes. O texto esperado vem do guião de gravação; os nomes de projeto são resolvidos em tempo de execução a partir da configuração local do projeto de referência e nunca são escritos em ficheiros versionados.

**Normalizador (um só, para todos os motores e variantes).** Unicode NFC; minúsculas (os acentos mantêm-se); hífen entre letras passa a espaço; restante pontuação e símbolos são apagados; espaços colapsados; lista pequena de equivalências técnicas (por exemplo `vs code`, `visual studio code` e `vscode` contam como o mesmo). Limites conhecidos: `c++` fica `c`; números em algarismos não coincidem com números por extenso. Detalhe em [bench/README.md](../../bench/README.md#normalizer).

**WER.** Taxa de erro de palavras do corpus: soma das edições (substituições, omissões, inserções) a dividir pela soma das palavras de referência, depois de normalizar.

**Erro em termos ingleses e em nomes.** Recall por ocorrência: cada ocorrência de um termo da lista genérica `bench/terms_en.txt` (ou de um nome de projeto) na referência conta uma vez; está certa quando a hipótese normalizada a contém pelo menos as mesmas vezes. Erro = 1 − encontradas / esperadas. Com só 17 ocorrências de termos, cada termo falhado vale cerca de 5,9 pontos percentuais; com 38 ocorrências de nomes, cada nome vale cerca de 2,6 pontos.

**Intenção preservada.** Uma frase conta como preservada só quando (1) todos os nomes de projeto e termos da lista que aparecem na referência aparecem na hipótese, e (2) um juiz local (Ollama `qwen3:8b`, sem raciocínio, temperatura 0, resposta JSON) responde "sim" a um prompt fixo: quem agisse com base na hipótese faria a mesma ação, com o mesmo alvo, nomes, termos, negação e números. O juiz corre no PC, por isso os textos não saem da máquina para serem julgados. A tabela verificável por uma pessoa (referência, hipótese, veredicto, motivo e coluna vazia para revisão humana) existe só em `bench/results/`, que é ignorado pelo Git.

**Latência.** Os takes são comandos curtos, por isso a latência mede-se em 10 compósitos de 5 a 15 s, feitos a partir dos mesmos takes reais concatenados com 0,3 s de ruído baixo (voz real, nunca sintética; construção determinística). O relógio arranca quando o compósito inteiro está em memória (equivalente a largar a tecla) e para quando existe o texto final: chamada ao motor, incluindo a rede nos motores cloud, mais a limpeza nas variantes com limpeza. Cada compósito corre 2 vezes por motor e variante (20 amostras); p50 e p95 são percentis por posição. O carregamento do modelo e a primeira chamada de aquecimento são medidos à parte. As esperas de ritmo dos planos gratuitos acontecem antes de o relógio arrancar e uma amostra que precisou de nova tentativa HTTP é medida de novo. É uma chamada por pedido completo (sem streaming): o texto aparece de uma vez no fim.

**Variantes.** Cada motor corre em quatro variantes: `raw` (sem ajudas), `hints` (vocabulário: nomes de projeto resolvidos em tempo de execução mais os termos genéricos, passado como `initial_prompt`/`hotwords` no Whisper local, `prompt` no Groq, `custom_vocabulary` no Gemini), `raw+cleanup` e `hints+cleanup` (a saída passa pelo `qwen3:8b` local com um prompt fixo de pontuação, maiúsculas, remoção de bengalas e repetições, sem traduzir nem acrescentar; na variante com hints o prompt também recebe o vocabulário).

**Motores.** Whisper large-v3 e large-v3-turbo locais (faster-whisper 1.1.1, CTranslate2 4.8.2, CUDA 12.8, float16, beam 5, na RTX 5060 Ti 16 GB); Groq `whisper-large-v3` e `whisper-large-v3-turbo` (plano gratuito); Gemini `gemini-3.5-transcribe` (plano gratuito). O `gemini-2.5-flash` previsto já não é oferecido a chaves novas (HTTP 404, 2026-09-28), por isso foi usado o modelo de transcrição dedicado da Google, através da Interactions API documentada para ele (`language_codes` `pt-PT`, modo literal por omissão, vocabulário em `custom_vocabulary`, `store: false` para o pedido não ficar guardado no servidor). O Deepgram Nova-3 não foi medido: por decisão do Sponsor (2026-09-28) não foi criada conta nem chave, e aparece como SKIPPED com esse motivo.

## Resultados

Tabela gerada a partir de [engines-summary.json](engines-summary.json) por `py -3.12 -m bench.report --write docs/research/ENGINES.md`; `bench.report --check` confirma que coincide com o resumo.

<!-- bench:summary:start lang=pt -->
| Motor | Variante | n | WER | Erro termos EN | Erro nomes | Intenção preservada | p50 (s) | p95 (s) | Estado |
|---|---|---|---|---|---|---|---|---|---|
| faster-whisper-large-v3 | raw | 44 | 34,2 % | 70,6 % | 52,6 % | 25,0 % | 0,93 | 1,24 | ok |
| faster-whisper-large-v3 | hints | 44 | 28,3 % | 23,5 % | 21,1 % | 52,3 % | 1,00 | 1,30 | ok |
| faster-whisper-large-v3 | raw+cleanup | 44 | 34,7 % | 70,6 % | 52,6 % | 25,0 % | 1,72 | 2,25 | ok |
| faster-whisper-large-v3 | hints+cleanup | 44 | 28,1 % | 23,5 % | 21,1 % | 47,7 % | 1,81 | 2,37 | ok |
| faster-whisper-large-v3-turbo | raw | 44 | 66,4 % | 88,2 % | 55,3 % | 11,4 % | 0,36 | 0,50 | ok |
| faster-whisper-large-v3-turbo | hints | 44 | 30,6 % | 52,9 % | 21,1 % | 38,6 % | 0,31 | 0,38 | ok |
| faster-whisper-large-v3-turbo | raw+cleanup | 44 | 160,0 % | 88,2 % | 55,3 % | 13,6 % | 1,16 | 1,53 | ok |
| faster-whisper-large-v3-turbo | hints+cleanup | 44 | 30,0 % | 52,9 % | 18,4 % | 38,6 % | 0,93 | 1,62 | ok |
| groq-whisper-large-v3 | raw | 44 | 35,6 % | 64,7 % | 55,3 % | 25,0 % | 0,59 | 1,35 | ok |
| groq-whisper-large-v3 | hints | 44 | 30,0 % | 23,5 % | 50,0 % | 29,5 % | 0,61 | 0,74 | ok |
| groq-whisper-large-v3 | raw+cleanup | 44 | 37,2 % | 64,7 % | 55,3 % | 25,0 % | 1,53 | 1,90 | ok |
| groq-whisper-large-v3 | hints+cleanup | 44 | 28,9 % | 23,5 % | 42,1 % | 31,8 % | 1,52 | 1,87 | ok |
| groq-whisper-large-v3-turbo | raw | 44 | 40,6 % | 82,3 % | 57,9 % | 18,2 % | 0,39 | 0,49 | ok |
| groq-whisper-large-v3-turbo | hints | 44 | 30,8 % | 58,8 % | 23,7 % | 38,6 % | 0,42 | 0,49 | ok |
| groq-whisper-large-v3-turbo | raw+cleanup | 44 | 41,4 % | 82,3 % | 57,9 % | 13,6 % | 1,33 | 1,60 | ok |
| groq-whisper-large-v3-turbo | hints+cleanup | 44 | 30,3 % | 58,8 % | 18,4 % | 40,9 % | 1,35 | 1,80 | ok |
| gemini-gemini-3.5-transcribe | raw | 0 | — | — | — | — | — | — | SKIPPED: skipped: free tier limit of 25 requests/day reached after 20 of 44 takes (HTTP 429, 2026-09-28) |
| gemini-gemini-3.5-transcribe | hints | 0 | — | — | — | — | — | — | SKIPPED: skipped: free tier limit of 25 requests/day reached after 20 of 44 takes (HTTP 429, 2026-09-28) |
| gemini-gemini-3.5-transcribe | raw+cleanup | 0 | — | — | — | — | — | — | SKIPPED: base variant skipped: skipped: free tier limit of 25 requests/day reached after 20 of 44 takes (HTTP 429, 2026-09-28) |
| gemini-gemini-3.5-transcribe | hints+cleanup | 0 | — | — | — | — | — | — | SKIPPED: base variant skipped: skipped: free tier limit of 25 requests/day reached after 20 of 44 takes (HTTP 429, 2026-09-28) |
| deepgram-nova-3 | raw | 0 | — | — | — | — | — | — | SKIPPED: skipped: not run by Sponsor decision (2026-09-28); no Deepgram account or key created |
| deepgram-nova-3 | hints | 0 | — | — | — | — | — | — | SKIPPED: skipped: not run by Sponsor decision (2026-09-28); no Deepgram account or key created |
| deepgram-nova-3 | raw+cleanup | 0 | — | — | — | — | — | — | SKIPPED: base variant skipped: skipped: not run by Sponsor decision (2026-09-28); no Deepgram account or key created |
| deepgram-nova-3 | hints+cleanup | 0 | — | — | — | — | — | — | SKIPPED: base variant skipped: skipped: not run by Sponsor decision (2026-09-28); no Deepgram account or key created |
<!-- bench:summary:end -->

Leitura dos resultados:

- **O vocabulário é o que mais conta.** No large-v3 local, `hints` baixa o WER de 34,2 % para 28,3 %, o erro em termos de 70,6 % para 23,5 % e o erro em nomes de 52,6 % para 21,1 %. No turbo local o WER cai de 66,4 % para 30,6 %. No Groq `whisper-large-v3` o vocabulário corrige os termos (23,5 %) mas pouco os nomes (50,0 %); no Groq turbo acontece o contrário (termos 58,8 %, nomes 23,7 %).
- **Mesmo modelo, resultados diferentes.** O large-v3 local e o do Groq são o mesmo modelo, mas o local usa beam 5 e recebe o vocabulário por `initial_prompt` e `hotwords`, enquanto o Groq só aceita `prompt` e não publica os parâmetros de descodificação. Com vocabulário, o local erra menos nomes (21,1 % contra 50,0 %) e preserva mais intenção (52,3 % contra 29,5 %).
- **A limpeza local quase não muda o WER** (28,3 % → 28,1 % no large-v3 com vocabulário) e baixa ligeiramente a intenção (52,3 % → 47,7 %), porque neste guião não há bengalas nem repetições para tirar. Soma entre 0,6 e 1,3 s ao p50 e ao p95 e empurra todas as variantes acima de 1,5 s. O seu valor tem de ser medido na Fase 2, com ditado real com hesitações.
- **Alucinação do turbo sem vocabulário.** No turbo local `raw`, um take entrou num ciclo de repetição (o modelo repete a mesma frase muitas vezes); é sobretudo esse take que explica o WER de 66,4 %. Na variante `raw+cleanup` o `qwen3:8b` alongou ainda mais esse texto e o WER chegou a 160 % (mais palavras inseridas do que palavras de referência). O harness passou a limitar o tamanho da resposta da limpeza ao tamanho da entrada. Com vocabulário, o ciclo não apareceu.
- **Intenção preservada baixa em todos.** No large-v3 local com vocabulário, dos 44 takes, 23 foram preservados, 10 falharam a regra de nomes/termos e 11 foram rejeitados pelo juiz. O juiz é exigente em comandos curtos, em que uma palavra errada muda o alvo; a tabela para revisão humana está em `bench/results/`.
- **Gemini, só indicativo (não entra na tabela).** Antes do limite de 25 pedidos por dia, o Gemini transcreveu os primeiros 20 takes sem vocabulário (220 palavras de referência). Nesses mesmos 20 takes: Gemini 28,7 % de WER; large-v3 local 22,6 % sem e 19,1 % com vocabulário; Groq large-v3 25,2 % sem e 22,2 % com vocabulário. Termos encontrados: Gemini 4 de 9 (large-v3 local com vocabulário 6 de 9, Groq large-v3 com vocabulário 7 de 9); nomes: Gemini 9 de 22 (variantes com vocabulário 19 de 22). Nesta amostra o Gemini não fica à frente, nem sequer das variantes sem vocabulário.
- **Latência.** O turbo local com vocabulário é o mais rápido (p95 0,38 s); o large-v3 local fica em 1,30 s e o Groq large-v3 em 0,74 s. Tempos de arranque medidos à parte: carregar o large-v3 3,5 s e primeira chamada 1,0 s; turbo 1,5 s e 0,3 s; `qwen3:8b` 4,8 s de carga e 5,2 s na primeira chamada.

## Resultados anteriores do projeto de referência (baseline, não medidos de novo)

O projeto de voz anterior do Sponsor mediu, em 2026-09-25, na mesma voz em português: Whisper medium 46,3 % de WER, Whisper large-v3-turbo 39,9 % e Parakeet 38,2 %. Ficam aqui como referência histórica; não são medições novas e não usam o normalizador deste benchmark, por isso a comparação com a tabela acima é só indicativa.

## VRAM e partilha do Ollama

A GPU tem 16 311 MiB. O harness tirou cinco fotografias da VRAM e dos modelos carregados no Ollama durante a execução final (2026-09-28, 12:19 a 12:28):

| Momento | VRAM usada | Modelos no Ollama |
|---|---|---|
| Início | 7 798 MiB | `qwen3:8b` (5 320 MiB) |
| Antes da limpeza | 2 447 MiB | nenhum (o modelo já tinha saído da memória; o harness nunca descarrega modelos) |
| Depois da limpeza | 8 084 MiB | `qwen3:8b` (5 320 MiB) |
| Antes do juiz de intenção | 8 084 MiB | `qwen3:8b` (5 320 MiB) |
| Depois do juiz de intenção | 8 385 MiB | `qwen3:8b` (5 320 MiB) |

- **Contenção observada: nenhuma.** Em nenhuma fotografia estava carregado o `qwen3:14b` nem outro modelo de outro projeto, por isso os números acima não sofreram com a partilha. O harness regista e reporta esta contenção quando ela acontece.
- **Ocupação estimada.** O large-v3 em float16 ocupa cerca de 4 GB e o turbo cerca de 2,2 GB. Large-v3 + `qwen3:8b` + o que o Windows e outros programas usam dá um pico de cerca de 11,8 GB, com margem de uns 4,5 GB. O `qwen3:14b` (cerca de 10 GB) não cabe ao mesmo tempo: se outro projeto o carregar, o Ollama descarrega modelos ou divide-os entre GPU e CPU e a limpeza fica muito mais lenta.
- **Arranque a frio.** O Ollama tira o `qwen3:8b` da memória quando fica parado algum tempo; a primeira limpeza depois disso custa cerca de 10 s (4,8 s de carga e 5,2 s na primeira chamada). Sem limpeza no caminho crítico, o Quill só precisa do Whisper em memória.

## Custos para cerca de 15 h/mês

Cenário: 30 min de ditado por dia, cerca de 15 h (900 min) por mês. Preços em USD tirados das páginas oficiais, consultadas a 2026-09-28. Não incluem IVA nem câmbio.

| Motor | Preço oficial | Faturação mínima por pedido | 15 h/mês | Fonte |
|---|---|---|---|---|
| Whisper local (large-v3 ou turbo) + `qwen3:8b` local | 0 | — | 0 EUR (só eletricidade) | — |
| Groq `whisper-large-v3` | 0,111 USD/h | 10 s por pedido | 1,67 USD; 3,33 USD se a frase média tiver 5 s; até 8,33 USD se forem comandos de 2 s | [Speech to Text](https://console.groq.com/docs/speech-to-text) (2026-09-28) |
| Groq `whisper-large-v3-turbo` | 0,04 USD/h | 10 s por pedido | 0,60 USD; 1,20 USD com frases de 5 s; até 3,00 USD com comandos de 2 s | [Speech to Text](https://console.groq.com/docs/speech-to-text) (2026-09-28) |
| Gemini `gemini-3.5-transcribe` (pago) | entrada 2,00 USD/1M tokens (≈0,003 USD/min de áudio a 25 tokens/s); saída 12,00 USD/1M (≈0,002 USD/min); ≈0,005 USD/min | não indicada (por tokens) | ≈4,50 USD de áudio e texto; a instrução de cada pedido (≈75 tokens, ≈160 com vocabulário) soma ≈1 a 3,5 USD conforme o número de pedidos | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) (2026-09-28) |
| Deepgram Nova-3 monolingue (não medido) | pré-gravado 0,0043 USD/min; streaming 0,0048 USD/min (promoção; normal 0,0077); keyterm +0,0013 USD/min | a página não indica mínimo por pedido | pré-gravado 3,87 USD (+1,17 com keyterm); streaming 4,32 USD (6,93 ao preço normal) | [Deepgram pricing](https://deepgram.com/pricing) (2026-09-28) |
| Wispr Flow Pro (referência) | 15 USD/mês (mensal) ou 12 USD/mês (anual) | — | 12 a 15 USD | [Wispr Flow pricing](https://wisprflow.ai/pricing) (2026-09-28) |

O mínimo de 10 s do Groq pesa em ditado curto: cada pedido de 3 s paga 10 s. As estimativas acima dão o intervalo entre áudio faturado ao segundo e frases curtas.

### Limites gratuitos e uso dos dados

| Fornecedor | Plano gratuito | O áudio pode ser usado para treino? | Fonte |
|---|---|---|---|
| Groq | Por modelo Whisper: 20 pedidos/min, 2 000 pedidos/dia, 7 200 s de áudio/hora, 28 800 s de áudio/dia (30 min/dia = 1 800 s, cerca de 6 % do limite diário); ficheiros até 25 MB | Por omissão o Groq não retém os dados de inferência; pode guardar entradas e saídas até 30 dias só para falhas de fiabilidade ou abuso. A página não menciona uso para treino. | [Rate limits](https://console.groq.com/docs/rate-limits), [Your data](https://console.groq.com/docs/your-data) (2026-09-28) |
| Gemini | Gratuito (entrada e saída), com limites por projeto visíveis só no AI Studio; o limite diário reinicia à meia-noite do Pacífico | **Sim** no plano gratuito ("Used to improve our products: Yes"); os termos dizem que revisores humanos podem ler, anotar e processar as entradas e saídas e pedem para não enviar dados sensíveis ou confidenciais. No plano pago: não. | [Pricing](https://ai.google.dev/gemini-api/docs/pricing), [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [Terms](https://ai.google.dev/gemini-api/terms) (2026-09-28) |
| Deepgram | 200 USD de crédito inicial, sem cartão | **Sim por omissão**: o programa Model Improvement Partnership está ligado; desliga-se por pedido com `mip_opt_out=true` | [Pricing](https://deepgram.com/pricing), [Model Improvement Partnership](https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program) (2026-09-28) |
| Local | Sem limites | Não: o áudio não sai do PC | — |

## Cenários de orçamento

Para 30 min de ditado por dia (cerca de 15 h/mês). Números medidos nos 44 takes, com vocabulário.

| Cenário | Motor | Custo mensal | WER | Erro termos EN | Erro nomes | Intenção | p95 | Observações |
|---|---|---|---|---|---|---|---|---|
| 0 EUR, local | Whisper large-v3 local | 0 EUR (só eletricidade) | 28,3 % | 23,5 % | 21,1 % | 52,3 % | 1,30 s | O áudio não sai do PC; funciona sem internet; ocupa cerca de 4 GB de VRAM partilhada. Com limpeza `qwen3:8b`: 28,1 % e p95 2,37 s. |
| 0 EUR, local rápido | Whisper large-v3-turbo local | 0 EUR | 30,6 % | 52,9 % | 21,1 % | 38,6 % | 0,38 s | Mais rápido e leve (cerca de 2,2 GB), mas falha mais termos ingleses; sem vocabulário pode alucinar. |
| Cloud gratuita | Groq `whisper-large-v3-turbo` (plano gratuito) | 0 USD | 30,8 % | 58,8 % | 23,7 % | 38,6 % | 0,49 s | 30 min/dia usa cerca de 6 % do limite diário de áudio; sem retenção por omissão; depende da rede e de um plano que pode mudar. |
| Cloud gratuita | Gemini `gemini-3.5-transcribe` (plano gratuito) | 0 USD | não medido (n=0) | — | — | — | — | 25 pedidos por dia não chegam para ditado; os dados podem ser usados pela Google e lidos por revisores. Inviável. |
| Pago mais barato | Groq `whisper-large-v3-turbo` (pago) | 0,60 a 3,00 USD | 30,8 % | 58,8 % | 23,7 % | 38,6 % | 0,49 s | Mesmos números do plano gratuito; o mínimo de 10 s por pedido faz o custo subir com frases curtas. |
| Pago perto de 6 USD | Groq `whisper-large-v3` (pago) | 1,67 a 8,33 USD | 30,0 % | 23,5 % | 50,0 % | 29,5 % | 0,74 s | Melhor em termos ingleses, pior em nomes do que o turbo. |
| Pago perto de 6 USD | Deepgram Nova-3 | 3,87 a 6,93 USD | não medido | — | — | — | — | Não medido por decisão do Sponsor; o áudio entra no programa de melhoria dos modelos, salvo `mip_opt_out=true`. |
| Pago perto de 6 USD | Gemini `gemini-3.5-transcribe` (pago) | cerca de 5,5 a 8 USD | não medido nos 44 | — | — | — | — | Em 20 takes, sem vocabulário, ficou atrás do Whisper local e do Groq (ver notas dos resultados). |
| Referência | Wispr Flow Pro | 12 a 15 USD | não medido | — | — | — | — | Produto fechado, não testado com esta voz; serve só de comparação de preço. |

Nenhum cenário pago mediu melhor do que o Whisper large-v3 local. Pagar pouparia VRAM e baixaria a latência face ao large-v3 local (p95 0,49 a 0,74 s contra 1,30 s), mas não traria mais precisão; o turbo local já é mais rápido do que qualquer opção cloud (p95 0,38 s).

## Riscos

- **Planos gratuitos mudam sem aviso.** Durante este benchmark o `gemini-2.5-flash` deixou de estar disponível para chaves novas. Um motor gratuito na cloud pode desaparecer, ficar mais lento ou passar a ter limites mais baixos; o produto precisa de um motor de recurso.
- **Uso dos dados.** No Gemini gratuito, o áudio e o texto podem ser usados pela Google para melhorar produtos e lidos por revisores humanos; no Deepgram o áudio entra no programa de melhoria dos modelos, salvo `mip_opt_out=true`. O Groq não retém por omissão. Ditar e-mails ou mensagens pessoais num plano gratuito da Google expõe esse conteúdo.
- **Latência de rede.** Os valores cloud medidos aqui dependem da ligação desta casa e da carga do fornecedor a 2026-09-28; um p95 medido num dia não garante o p95 de outro dia. Os motores locais não dependem da rede.
- **VRAM partilhada.** A GPU de 16 GB é partilhada com outros projetos que usam o Ollama e às vezes carregam o `qwen3:14b`. Whisper large-v3 (cerca de 3 a 4 GB), `qwen3:8b` (cerca de 6 GB) e `qwen3:14b` (cerca de 10 GB) não cabem todos ao mesmo tempo; se não couberem, o Ollama descarrega ou divide modelos entre GPU e CPU e a latência sobe.
- **Guião curto de comandos contra ditado real.** As 44 gravações são comandos de 0,6 a 6,3 s, não ditado contínuo. O WER em ditado longo, com hesitações, pode ser diferente, e a latência de 5 a 15 s foi medida em compósitos destes comandos. A amostra é pequena: 17 ocorrências de termos ingleses e 38 de nomes dão margens largas.
- **Remoção de bengalas e repetições não mensurável.** O guião não tem bengalas ("hum") nem repetições, por isso a meta de remover pelo menos 95 % sem apagar conteúdo não pôde ser medida. A Fase 2 precisa de um guião de ditado com hesitações e de um gravador.
- **Juiz de intenção automático.** O juiz local (`qwen3:8b`) pode errar; a tabela para revisão humana existe em `bench/results/` e deve ser revista antes de fixar metas.

## Decisão (a aguardar o Sponsor)

**Estado: a aguardar a decisão do Sponsor.** Nada foi adotado nem pago e não foi escrito código de produto.

| Opção | Motor | Custo mensal (15 h) | WER | Erro termos EN | Erro nomes | Intenção | p95 |
|---|---|---|---|---|---|---|---|
| **A. Melhor a 0 EUR (recomendada)** | Whisper large-v3 local com vocabulário | 0 EUR | 28,3 % | 23,5 % | 21,1 % | 52,3 % | 1,30 s |
| B. Melhor paga | Groq `whisper-large-v3-turbo` com vocabulário (gratuita até ao limite, depois paga) | 0,60 a 3,00 USD | 30,8 % | 58,8 % | 23,7 % | 38,6 % | 0,49 s |
| Referência | Wispr Flow Pro | 12 a 15 USD | não medido | — | — | — | — |

**Recomendação: opção A.** É a mais precisa de todas as medidas, custa 0 EUR, o áudio nunca sai do PC e cumpre a meta de latência p95 ≤ 1,5 s (1,30 s). A opção B só compensa se a GPU estiver muitas vezes ocupada por outros projetos ou se a latência for mais importante do que os nomes e termos; pode ficar como motor de recurso, mas isso também é uma decisão do Sponsor, porque o áudio sairia do PC.

Condições da recomendação:

- A limpeza com `qwen3:8b` fica fora do caminho crítico até ser medida com ditado real com hesitações; neste guião não melhora o WER e passa a latência para 2,37 s.
- Nenhum motor está perto das metas por omissão da Fase 2 (WER ≤ 10 %, intenção ≥ 95 %). A distância terá de ser reduzida pelas camadas de adaptação (vocabulário pessoal e aprendizagem de correções) e medida de novo com a voz do Sponsor. Antes de fixar as metas da Fase 2, a tabela de intenção em `bench/results/` deve ser revista à mão.
- A Fase 2 precisa de um guião de ditado com bengalas e repetições e de um gravador de um só comando para medir a remoção de bengalas.

**O que o Sponsor tem de decidir:** o motor (A, B ou outro) e o custo mensal aceite. Silêncio ou retoma não contam como aprovação; nenhum serviço pago é adotado sem essa decisão.
