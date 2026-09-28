# Quill: medições da Fase 2 com a voz real

**Estado: baseline medida a 2026-09-28** (tarefa T2). Motor decidido pelo Sponsor: Whisper large-v3 local (faster-whisper, float16, CUDA, RTX 5060 Ti) com vocabulário (`initial_prompt` e `hotwords`), 0 EUR/mês; o áudio não sai do PC. As etapas seguintes (limpeza, vocabulário pessoal, correções, perfis) acrescentam linhas a este documento.

Este documento contém só números agregados: nenhum texto falado, nenhum nome de projeto real e nenhum caminho da máquina. Os resultados por frase ficam apenas em `bench/results/`, que o Git ignora. Os números vêm de [phase2-summary.json](phase2-summary.json); o harness está em [bench/](../../bench/README.md#pipeline-evaluation).

## Resumo

- **Ditado (36 gravações novas):** WER 13,5 % contra a referência limpa e 16,9 % contra a literal; erro em termos ingleses 2,3 %; erro em nomes de projeto 0,0 %; intenção preservada 55,6 %; o próprio Whisper já tira 64,3 % das hesitações e repetições; faltam 19 de 960 palavras de conteúdo.
- **Comandos (44 gravações da Fase 1):** WER 28,3 %, termos 23,5 %, nomes 21,1 %, intenção 52,3 %: exatamente os valores da Fase 1 para large-v3 com vocabulário, o que confirma que o harness reproduz a medição.
- **Metas já cumpridas na baseline:** erro em termos e em nomes ≤ 10 % no conjunto de ditado.
- **Metas por cumprir:** WER final ≤ 10 % e intenção ≥ 95 % nos dois conjuntos; remoção de hesitações ≥ 95 % sem apagar conteúdo; termos e nomes ≤ 10 % no conjunto de comandos. A latência da aplicação, a digitação sem perdas e a aprendizagem de correções ainda não têm medição (tarefas T3, T4 e T7).

## Conjuntos

| Conjunto | Gravações válidas | Áudio total | Duração por gravação | Com mais de 15 s | Ocorrências de termos EN | Ocorrências de nomes |
|---|---|---|---|---|---|---|
| comandos | 44 de 44 | 104,0 s | 0,64 a 6,27 s (mediana 2,11 s) | 0 | 17 | 38 |
| ditado | 36 de 36 (mínimo 30) | 337,9 s | 4,37 a 18,05 s (mediana 8,56 s) | 6 | 44 | 9 |

Todas as gravações são da voz real do Sponsor, 16 kHz mono, pelo microfone do headset (origem `microfone` em todas); nenhuma é sintética. O conjunto de ditado segue o guião `bench/dictation/guiao-ditado-pt.md` (estilos Claude Code, VS Code, WhatsApp e email) e tem 56 trechos marcados de hesitação ou repetição (36 hesitações e 20 repetições) e 960 palavras de conteúdo. Nenhuma gravação foi rejeitada nem descartada no ditado; nos comandos, os 44 takes descartados (`*.invalida-*`) ficam de fora, como na Fase 1.

Com poucas ocorrências, cada falha pesa muito: no ditado cada termo falhado vale cerca de 2,3 pontos percentuais e cada nome cerca de 11,1 pontos; nos comandos cada termo vale cerca de 5,9 pontos e cada nome cerca de 2,6.

## Método

`bench.pipeline --stage raw` carrega o large-v3 uma vez, faz uma chamada de aquecimento e transcreve todas as gravações válidas dos dois conjuntos com o vocabulário (nomes de projeto resolvidos em tempo de execução e a lista genérica `bench/terms_en.txt`). O normalizador, o WER, o erro em termos e nomes e o juiz de intenção são os da Fase 1 (ver [ENGINES.md](ENGINES.md#método)).

- **WER literal** compara com tudo o que foi dito (hesitações e repetições incluídas); **WER limpo** compara com o texto sem elas, que é o texto final desejado. Nos comandos, sem marcação, os dois são iguais.
- **Hesitações removidas:** percentagem dos trechos marcados cujas palavras estão todas ausentes da transcrição. **Palavras apagadas:** palavras de conteúdo sem correspondência na transcrição (uma palavra trocada é um erro de reconhecimento, não uma palavra apagada).
- **Intenção preservada:** regra de nomes e termos mais o juiz local `qwen3:8b`, contra a referência limpa.
- **p95 transcrição:** tempo de uma chamada ao motor por gravação, com o modelo já quente. Não é a latência da aplicação (largar a tecla até ao texto escrito), que a tarefa T4 mede.

## Baseline (etapa `raw`)

Tabela gerada a partir de [phase2-summary.json](phase2-summary.json) por `py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --write-doc docs/research/FASE2.md`; `--check-doc` confirma que coincide com o resumo.

<!-- pipeline:summary:start -->
| Conjunto | Etapa | n | WER literal | WER limpo | Erro termos EN | Erro nomes | Intenção preservada | Hesitações removidas | Palavras apagadas |
|---|---|---|---|---|---|---|---|---|---|
| comandos | raw | 44 | 28,3 % | 28,3 % | 23,5 % | 21,1 % | 52,3 % | — | — |
| ditado | raw | 36 | 16,9 % | 13,5 % | 2,3 % | 0,0 % | 55,6 % | 64,3 % | 19 |
<!-- pipeline:summary:end -->

Leitura dos resultados:

- **O ditado longo transcreve melhor do que os comandos curtos.** WER limpo 13,5 % contra 28,3 %; termos 2,3 % contra 23,5 %; nomes 0,0 % contra 21,1 %. Frases mais longas dão contexto ao modelo; nos comandos, uma palavra errada pesa muito mais.
- **O Whisper já filtra parte das hesitações.** Sem qualquer limpeza, 36 dos 56 trechos marcados (64,3 %) não aparecem na transcrição; é por isso que o WER limpo (13,5 %) é mais baixo do que o literal (16,9 %). Os restantes 20 trechos são o trabalho da limpeza (T5).
- **As 19 palavras apagadas (2,0 % das 960) são omissões do reconhecimento**, não de uma limpeza, porque esta etapa ainda não limpa nada. São o ponto de partida da meta de 0 palavras apagadas: a limpeza não pode apagar mais, mas estas omissões só baixam com melhor reconhecimento.
- **Intenção preservada baixa nos dois conjuntos.** No ditado, 20 de 36 frases foram preservadas, 1 falhou a regra de nomes e termos e 15 foram rejeitadas pelo juiz. Nos comandos, 23 de 44, 10 pela regra e 11 pelo juiz (igual à Fase 1). O juiz ainda não foi revisto à mão; a tabela para revisão humana está em `bench/results/`.
- **Reprodutibilidade.** A etapa foi corrida quatro vezes: as transcrições e os veredictos de intenção foram iguais em todas as gravações, por isso todas as métricas de palavras (WER, termos, nomes, hesitações, palavras apagadas, intenção) deram os mesmos valores. Só os tempos variam, e muito, com a carga da GPU, que é partilhada com outros programas: p95 de transcrição nos comandos 0,57, 0,59, 0,78 e 1,17 s; no ditado 1,20, 1,39, 4,23 e 2,29 s. A coluna de p95 na tabela acima é a da execução que gerou o resumo.
- **Latência por duração.** No ditado, nas 30 gravações até 15 s, o p95 de transcrição foi 1,05 e 1,15 s nas duas execuções com a GPU livre; nas duas execuções com a GPU ocupada foi 4,23 e 2,06 s. O p95 dos comandos é por gravação curta e não é comparável com o 1,30 s da Fase 1, medido em compósitos de 5 a 15 s.

## Distância às metas da Fase 2

Valores da etapa `raw`. As metas não baixam; cada linha diz que tarefa a trata.

| Meta | Comandos | Ditado | Distância | Tarefa |
|---|---|---|---|---|
| WER final (referência limpa) ≤ 10 % | 28,3 % | 13,5 % | comandos +18,3 pp; ditado +3,5 pp | T5 a T10 |
| Intenção preservada ≥ 95 % | 52,3 % | 55,6 % | comandos −42,7 pp; ditado −39,4 pp | T5 a T10 |
| Erro em nomes de projeto ≤ 10 % | 21,1 % | 0,0 % | comandos +11,1 pp; ditado cumprida | T6 |
| Erro em termos ingleses ≤ 10 % | 23,5 % | 2,3 % | comandos +13,5 pp; ditado cumprida | T6 |
| Hesitações e repetições removidas ≥ 95 % | não se aplica | 64,3 % | −30,7 pp (faltam 20 de 56 trechos) | T5 |
| Palavras de conteúdo apagadas = 0 | não se aplica | 19 | +19 palavras (omissões do reconhecimento) | T5 (não piorar), T6 |
| Latência p95 ≤ 1,5 s do largar da tecla ao texto (até 15 s) | não medida | não medida | só transcrição: 1,05 a 1,15 s no ditado até 15 s com a GPU livre (sobram cerca de 0,35 a 0,45 s para o fim da captura e a digitação), 2,06 a 4,23 s com a GPU ocupada por outros programas | T4 |
| 0 caracteres perdidos ou duplicados na digitação | não medido | não medido | — | T3 |
| Correções aprendidas: 100 % das repetições corrigidas, 0 erros novos | não medido | não medido | — | T7 |

A meta de intenção é a mais distante nos dois conjuntos. Antes de concluir que o motor não chega lá, a tabela do juiz deve ser revista à mão, porque o juiz é exigente e ainda não foi validado contra uma revisão humana. Se no fim das camadas de adaptação (T10) o WER final e a intenção continuarem fora das metas, o resultado vai ao Sponsor como decisão, com a distância medida e as opções, sem baixar as metas.
