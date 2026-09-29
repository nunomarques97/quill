# Quill: medições da Fase 2 com a voz real

**Estado: baseline medida a 2026-09-28** (tarefa T2); **transcrição por blocos medida a 2026-09-29** (tarefa T3, etapa `streamed`); **juiz de intenção revisto e limpeza do texto medida a 2026-09-29** (tarefa T4, etapa `cleanup`). Motor decidido pelo Sponsor: Whisper local (faster-whisper, float16, CUDA, RTX 5060 Ti) com vocabulário (`initial_prompt` e `hotwords`), 0 EUR/mês; o áudio não sai do PC. A baseline (`raw`) é do large-v3. Desde a decisão do Sponsor de 2026-09-29, o modelo por omissão da aplicação é o large-v3-turbo, porque só ele cumpre a meta de latência; o large-v3 fica como modo preciso (ver [Transcrição por blocos](#transcrição-por-blocos-etapa-streamed)). As etapas seguintes (vocabulário pessoal, correções, perfis) acrescentam linhas a este documento.

Este documento contém só números agregados: nenhum texto falado, nenhum nome de projeto real e nenhum caminho da máquina. Os resultados por frase ficam apenas em `bench/results/`, que o Git ignora. Os números vêm de [phase2-summary.json](phase2-summary.json); o harness está em [bench/](../../bench/README.md#pipeline-evaluation).

## Resumo

- **Ditado (36 gravações novas), baseline large-v3:** WER 13,5 % contra a referência limpa e 16,9 % contra a literal; erro em termos ingleses 2,3 %; erro em nomes de projeto 0,0 %; intenção preservada 47,2 % (com o juiz revisto na T4); o próprio Whisper já tira 64,3 % das hesitações e repetições; faltam 19 de 960 palavras de conteúdo.
- **Comandos (44 gravações da Fase 1), baseline large-v3:** WER 28,3 %, termos 23,5 %, nomes 21,1 %: exatamente os valores da Fase 1 para large-v3 com vocabulário, o que confirma que o harness reproduz a medição. A intenção era 52,3 % com o juiz da Fase 1 e é 45,5 % com o juiz revisto.
- **Juiz de intenção revisto (T4):** comparado com uma revisão manual das 80 frases da etapa `streamed`, o juiz antigo concordava em 67 (83,8 %) e era demasiado brando; o juiz revisto concorda em 71 (88,8 %). Ver [Revisão do juiz de intenção](#revisão-do-juiz-de-intenção).
- **Limpeza (T4, etapa `cleanup`):** as regras determinísticas removem 96,4 % das hesitações e repetições (meta ≥ 95 %) sem apagar nenhuma palavra de conteúdo (meta 0) e baixam o WER limpo do ditado de 13,0 % para 11,0 %. O `qwen3:8b` fica desligado: apagava 13 palavras de conteúdo e o p95 por frase era 0,97 s. Ver [Limpeza do texto](#limpeza-do-texto-etapa-cleanup).
- **Metas já cumpridas na baseline:** erro em termos e em nomes ≤ 10 % no conjunto de ditado.
- **Metas cumpridas pela limpeza:** hesitações e repetições removidas ≥ 95 % e 0 palavras de conteúdo apagadas pela limpeza.
- **Metas por cumprir:** WER final ≤ 10 % e intenção ≥ 95 % nos dois conjuntos; termos e nomes ≤ 10 % no conjunto de comandos e, com o large-v3-turbo, nomes no ditado (11,1 %). A digitação sem perdas e a aprendizagem de correções ainda não têm medição (tarefas T11 e T6).
- **Latência do largar do gatilho ao texto final (T3):** cumpre a meta de p95 ≤ 0,5 s com o large-v3-turbo, o novo motor por omissão; com o large-v3 nenhuma afinação a cumpria. Em troca, os comandos ficam piores com o turbo (termos ingleses 47,1 % e nomes 18,4 %), o que a T5 tem de recuperar (ver [Transcrição por blocos](#transcrição-por-blocos-etapa-streamed)).

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
- **Intenção preservada:** regra de nomes e termos, regra de frase cortada (desde a T4) e o juiz local `qwen3:8b` com o prompt revisto na T4, contra a referência limpa. Todas as etapas da tabela foram medidas de novo com este juiz.
- **Apagadas pela limpeza:** palavras de conteúdo que a etapa anterior tinha certas (alinhadas com a palavra igual) e que o texto limpo já não tem, apagadas ou trocadas. As omissões do reconhecimento continuam na coluna «Palavras apagadas» e não contam contra a limpeza.
- **p95 transcrição:** tempo de uma chamada ao motor por gravação, com o modelo já quente. Não é a latência da aplicação (largar o gatilho até ao texto final), que a tarefa T3 mede com `bench.streaming`.

## Baseline (etapa `raw`)

Tabela gerada a partir de [phase2-summary.json](phase2-summary.json) por `py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --write-doc docs/research/FASE2.md`; `--check-doc` confirma que coincide com o resumo.

<!-- pipeline:summary:start -->
| Conjunto | Etapa | n | WER literal | WER limpo | Erro termos EN | Erro nomes | Intenção preservada | Hesitações removidas | Palavras apagadas | Apagadas pela limpeza |
|---|---|---|---|---|---|---|---|---|---|---|
| comandos | raw | 44 | 28,3 % | 28,3 % | 23,5 % | 21,1 % | 45,5 % | — | — | — |
| comandos | streamed | 44 | 30,0 % | 30,0 % | 47,1 % | 18,4 % | 31,8 % | — | — | — |
| comandos | cleanup | 44 | 30,0 % | 30,0 % | 47,1 % | 18,4 % | 29,5 % | — | — | — |
| ditado | raw | 36 | 16,9 % | 13,5 % | 2,3 % | 0,0 % | 47,2 % | 64,3 % | 19 | — |
| ditado | streamed | 36 | 16,5 % | 13,0 % | 2,3 % | 11,1 % | 52,8 % | 64,3 % | 19 | — |
| ditado | cleanup | 36 | 17,8 % | 11,0 % | 2,3 % | 11,1 % | 55,6 % | 96,4 % | 20 | 0 |
<!-- pipeline:summary:end -->

Leitura dos resultados:

- **O ditado longo transcreve melhor do que os comandos curtos.** WER limpo 13,5 % contra 28,3 %; termos 2,3 % contra 23,5 %; nomes 0,0 % contra 21,1 %. Frases mais longas dão contexto ao modelo; nos comandos, uma palavra errada pesa muito mais.
- **O Whisper já filtra parte das hesitações.** Sem qualquer limpeza, 36 dos 56 trechos marcados (64,3 %) não aparecem na transcrição; é por isso que o WER limpo (13,5 %) é mais baixo do que o literal (16,9 %). Os restantes 20 trechos são o trabalho da limpeza (T4).
- **As 19 palavras apagadas (2,0 % das 960) são omissões do reconhecimento**, não de uma limpeza, porque esta etapa ainda não limpa nada. São o ponto de partida da meta de 0 palavras apagadas: a limpeza não pode apagar mais, mas estas omissões só baixam com melhor reconhecimento.
- **Intenção preservada baixa nos dois conjuntos.** Com o juiz revisto, no ditado 17 de 36 frases foram preservadas: 1 falhou a regra de nomes e termos, 1 estava cortada e 17 foram rejeitadas pelo juiz. Nos comandos, 20 de 44: 10 pela regra de nomes e termos, 2 cortadas e 12 pelo juiz. Com o juiz da Fase 1 eram 20 de 36 e 23 de 44.
- **Reprodutibilidade.** A etapa foi corrida quatro vezes: as transcrições e os veredictos de intenção foram iguais em todas as gravações, por isso todas as métricas de palavras (WER, termos, nomes, hesitações, palavras apagadas, intenção) deram os mesmos valores. Só os tempos variam, e muito, com a carga da GPU, que é partilhada com outros programas: p95 de transcrição nos comandos 0,57, 0,59, 0,78 e 1,17 s; no ditado 1,20, 1,39, 4,23 e 2,29 s. A coluna de p95 na tabela acima é a da execução que gerou o resumo.
- **Latência por duração.** No ditado, nas 30 gravações até 15 s, o p95 de transcrição foi 1,05 e 1,15 s nas duas execuções com a GPU livre; nas duas execuções com a GPU ocupada foi 4,23 e 2,06 s. O p95 dos comandos é por gravação curta e não é comparável com o 1,30 s da Fase 1, medido em compósitos de 5 a 15 s.

## Transcrição por blocos (etapa `streamed`)

Na aplicação, o texto é transcrito enquanto o gatilho está premido e, ao largar, só se transcreve o que ainda falta (`quill.streaming`, tarefa T3). A etapa `streamed` passa cada gravação real, lida no sítio, por esse mesmo código, num calendário por tempo de áudio: entra um bloco de 50 ms (um buffer da captura MME) e o motor acaba o que tem pendente antes do bloco seguinte. Por isso o texto final é reprodutível: duas execuções independentes (`bench.streaming` e `bench.pipeline`) deram as mesmas métricas. É este texto, e não o da etapa `raw`, que as etapas seguintes vão limpar e corrigir.

Afinação escolhida para o large-v3-turbo (registada em `engine.streaming` no resumo): a cada 0,5 s de áudio com fala nova, um bloco com feixe 1 mostra as palavras ao vivo, sem as confirmar; ao largar, a fala inteira é transcrita com feixe 5, com o vocabulário. Como nada fica confirmado antes do fim, o texto final é tão bom como transcrever a gravação de uma vez. O silêncio inicial é cortado antes de cada transcrição, porque o Whisper inventa palavras no silêncio.

**Decisão do Sponsor (2026-09-29).** Nenhuma afinação do large-v3 chegou a p95 ≤ 0,5 s do largar ao texto final nas gravações até 15 s: além de transcrever o resto, é preciso esperar pelo bloco que já está a correr na GPU quando o gatilho é largado. A melhor afinação ficou em cerca de 0,9 s (piorando o ditado para 15,0 %) e a que mantinha a qualidade em cerca de 1,1 s. O Sponsor escolheu passar o motor por omissão para o large-v3-turbo, que já estava em `models/` (0 EUR, nada instalado), e manter a meta de 0,5 s. O large-v3 fica disponível como modo preciso em `local/quill.toml` (`[engine] model = "large-v3"`), com a afinação que confirma o texto nas pausas.

Leitura dos resultados (tabela acima; `streamed` é o large-v3-turbo):

- **Latência: cumprida.** Com as gravações reproduzidas ao ritmo real, o modelo quente e nenhum modelo do Ollama carregado na GPU, o p95 do largar ao texto final nas 74 gravações até 15 s foi 0,34 s nas duas execuções (máximo 0,43 s). Os tempos variam de execução para execução; o detalhe fica só em `bench/results/streaming/`.
- **Ditado: igual ou melhor do que a baseline.** WER limpo 13,0 % contra 13,5 %; termos iguais (2,3 %); intenção 52,8 % contra 47,2 %; as mesmas hesitações removidas e as mesmas 19 palavras apagadas. Um nome falhado em 9 dá 11,1 % de erro em nomes.
- **Comandos: piores do que a baseline.** WER 30,0 % contra 28,3 %; termos ingleses 47,1 % contra 23,5 % (8 de 17 falhados); nomes 18,4 % contra 21,1 %; intenção 31,8 % contra 45,5 %. Nas frases curtas, o turbo acerta menos nos termos ingleses.
- **Modo preciso (large-v3 com confirmação nas pausas), para comparação:** comandos WER 25,3 %, termos 23,5 %, nomes 15,8 %, intenção 52,3 %; ditado WER limpo 13,8 %, termos 0,0 %, nomes 0,0 %, hesitações removidas 67,9 %, 19 palavras apagadas, intenção 55,6 % (intenção medida com o juiz da Fase 1, antes da revisão da T4). Com a GPU livre, o p95 do largar ao texto final foi cerca de 1,1 s.
- **Confirmar mais cedo piora o texto.** No large-v3, as afinações que também confirmam as palavras em que dois blocos seguidos concordam deixam menos áudio para o fim, mas no ditado deram WER limpo entre 14,8 % e 19,1 %. No turbo, confirmar nas pausas também piorou (comandos 30,6 %, ditado 14,5 %), por isso fica desligado.
- **Rede de segurança (decisão do Sponsor).** Se, depois do vocabulário pessoal (T5), o erro em nomes de projeto ou em termos ingleses com o turbo continuar acima de 10 % em qualquer um dos conjuntos, isso é comunicado ao Sponsor para decidir se o motor por omissão volta ao large-v3.

## Revisão do juiz de intenção

O juiz de intenção é o oráculo de todas as etapas, por isso foi revisto antes de mexer na limpeza. As 80 frases da etapa `streamed` (44 comandos e 36 ditados) foram avaliadas à mão, uma a uma, com uma regra escrita: `sim` quando quem lê a transcrição recebe a mesma mensagem que a referência sem hesitações; `não` quando um pedido passa a afirmação ou muda de tempo, falta, sobra ou é trocada uma palavra com informação, ou a frase está cortada; pontuação, maiúsculas, hesitações, gralhas que se leem como a mesma palavra e artigos não contam. A revisão manual e as tabelas ficam só em `bench/results/intent-review/`, porque têm texto falado.

| Juiz | Comandos: concordam | Ditado: concordam | Total | Juiz sim, revisão não | Juiz não, revisão sim |
|---|---|---|---|---|---|
| Fase 1 | 38 de 44 | 29 de 36 | 67 de 80 (83,8 %) | 13 | 0 |
| Revisto (T4) | 41 de 44 | 30 de 36 | 71 de 80 (88,8 %) | 7 | 2 |

- **O juiz antigo era brando, não exigente.** As 13 discordâncias eram todas «juiz sim, revisão não»: frases cortadas no fim, um qualificador que faltava (um dia, um adjetivo), um pedido que passava a afirmação na primeira pessoa, um tempo verbal trocado e um substantivo trocado por outro. Os «não» do juiz estavam todos certos.
- **O que mudou.** (1) Uma regra determinística de frase cortada: se todos os alinhamentos de custo mínimo apagam a última palavra da referência, a frase não é preservada e o juiz nem é chamado. (2) Uma frase no prompt do juiz que nomeia os casos acima. Um prompt mais detalhado, com uma lista de regras, foi experimentado e piorou (56 de 80): o `qwen3:8b` passou a rejeitar hesitações, artigos e gralhas.
- **O que falta.** O juiz revisto ainda é um pouco mais brando do que a revisão (7 contra 2): dá como preservadas frases com uma palavra de informação trocada ou em falta a meio. Com o juiz revisto, a intenção preservada baixou em todas as etapas (tabela acima); a meta de 95 % continua igual.
- **Validação pelo Sponsor.** As tabelas `bench/results/intent-review/<execução>-streamed-commands.md` (comandos) e `...-streamed-dictation.md` (ditado) têm a referência, a transcrição, o veredicto e a razão do juiz, o veredicto e a razão da revisão e uma coluna Sponsor vazia; as instruções estão no topo de cada tabela.

## Limpeza do texto (etapa `cleanup`)

A limpeza corre depois da transcrição e antes da digitação (`quill/cleanup.py`). Por omissão são regras determinísticas, genéricas para português ditado e nunca escritas a partir do guião: tiram hesitações (`hum`, `hã`, `é pá`; `pronto` só no início de uma oração e não em «está pronto» ou «pronto para»; `tipo` só quando não é nome, como em «o tipo», «por tipo», «sem tipo», «tipo de» ou no fim de uma frase), tiram repetições imediatas de 1 a 4 palavras (exceto números, palavras de ênfase como «não, não» e pares válidos como «para para» ou «se se»), arrumam a pontuação e os espaços e põem maiúscula no início das frases. Termos ingleses, nomes e números nunca são alterados. Aplicar a limpeza duas vezes dá o mesmo texto.

A etapa `cleanup` aplica estas regras ao texto da etapa `streamed` (tabela acima):

- **Hesitações e repetições: 96,4 % removidas (meta ≥ 95 %, cumprida).** Dos 56 trechos marcados ficam 2, e ambos são erros de reconhecimento que nenhuma regra pode corrigir sem adivinhar: uma hesitação que o Whisper escreveu como uma palavra de conteúdo e uma repetição que ele transcreveu como palavras diferentes.
- **Palavras apagadas pela limpeza: 0 (meta 0, cumprida).** A coluna «Palavras apagadas» passa de 19 para 20 sem que a limpeza apague nada: numa frase, o Whisper escreveu uma hesitação no sítio de uma palavra que não reconheceu; a limpeza tira bem a hesitação e o alinhamento passa a contar essa palavra em falta como apagada em vez de trocada. É uma omissão do reconhecimento.
- **WER limpo do ditado: 13,0 % → 11,0 %.** O WER literal sobe (16,5 % → 17,8 %) porque as hesitações ditas deixam de aparecer, que é o objetivo. Nos comandos, sem hesitações marcadas, o WER não muda (30,0 %).
- **Intenção:** ditado 52,8 % → 55,6 %; comandos 31,8 % → 29,5 %, uma frase a menos. Nessa frase a limpeza só acrescentou o ponto final e o juiz mudou de veredicto; a revisão manual já a dava como não preservada.

**Regras contra o `qwen3:8b` local.** `bench.pipeline --compare-cleanup` limpa os mesmos textos `streamed` com as regras e com o `qwen3:8b` (que, em caso de falha do Ollama ou de resposta implausível, volta às regras):

| Ditado | WER limpo | Intenção | Hesitações removidas | Apagadas pela limpeza | Erro nomes | p50 / p95 por frase |
|---|---|---|---|---|---|---|
| Regras | 11,0 % | 55,6 % | 96,4 % | 0 | 11,1 % | menos de 1 ms |
| `qwen3:8b` | 11,6 % | 61,1 % | 98,2 % | 13 | 0,0 % | 0,53 s / 0,97 s |

Nos comandos, o `qwen3:8b` deu WER 28,9 % (regras 30,0 %), nomes 13,2 % (18,4 %), a mesma intenção (29,5 %) e p50/p95 de 0,20/0,38 s, com uma resposta rejeitada por ter o tamanho errado. **Decisão: o `qwen3:8b` fica desligado por omissão** (`[cleanup] mode = "rules"`): no ditado apaga ou troca 13 palavras de conteúdo que o reconhecimento tinha certas (a meta é 0) e piora o WER; a melhoria na intenção é medida por outro modelo e não compensa as palavras perdidas; e o tempo não cabe no orçamento: o p95 do largar ao texto final já é 0,34 s sem limpeza, e o modelo acrescenta 0,97 s no p95 do ditado (ordem de grandeza igual à da Fase 1, 0,6 a 1,3 s). Os tempos variam de execução para execução e ficam só em `bench/results/cleanup/`. O modo `llm` continua disponível em `local/quill.toml` para quem aceitar esse custo; com as regras, a limpeza leva menos de 1 ms por frase.

## Distância às metas da Fase 2

Valores da etapa `cleanup`, o texto final da aplicação até agora (large-v3-turbo e limpeza por regras); entre parênteses, a baseline `raw` do large-v3. Intenção medida com o juiz revisto. As metas não baixam; cada linha diz que tarefa a trata.

| Meta | Comandos | Ditado | Distância | Tarefa |
|---|---|---|---|---|
| WER final (referência limpa) ≤ 10 % | 30,0 % (28,3 %) | 11,0 % (13,5 %) | comandos +20,0 pp; ditado +1,0 pp | T5 a T7 |
| Intenção preservada ≥ 95 % | 29,5 % (45,5 %) | 55,6 % (47,2 %) | comandos −65,5 pp; ditado −39,4 pp | T5 a T7 |
| Erro em nomes de projeto ≤ 10 % | 18,4 % (21,1 %) | 11,1 % (0,0 %) | comandos +8,4 pp; ditado +1,1 pp (1 nome em 9) | T5 (rede de segurança) |
| Erro em termos ingleses ≤ 10 % | 47,1 % (23,5 %) | 2,3 % (2,3 %) | comandos +37,1 pp; ditado cumprida | T5 (rede de segurança) |
| Hesitações e repetições removidas ≥ 95 % | não se aplica | 96,4 % (64,3 %) | cumprida | T4 |
| Palavras de conteúdo apagadas pela limpeza = 0 | não se aplica | 0 | cumprida; as 20 omissões do reconhecimento continuam visíveis | T4 |
| Latência p95 ≤ 0,5 s do largar do gatilho ao texto final (até 15 s; meta do Sponsor) | cumprida | cumprida | cumprida com o large-v3-turbo nos dois conjuntos juntos, ao ritmo real e com a GPU livre; com o large-v3 (modo preciso) não; os tempos ficam só em `bench/results/` | T3 |
| 0 caracteres perdidos ou duplicados na digitação | não medido | não medido | — | T11 |
| Correções aprendidas: 100 % das repetições corrigidas, 0 erros novos | não medido | não medido | — | T6 |

A meta de intenção é a mais distante nos dois conjuntos. O juiz foi revisto à mão na T4 e era brando, não exigente: com o juiz revisto a distância aumentou, e a revisão manual dá valores ainda mais baixos (25,0 % nos comandos e 47,2 % no ditado, na etapa `streamed`). A distância vem sobretudo de erros de reconhecimento (palavras e nomes trocados), que a limpeza não corrige. Se no fim das camadas de adaptação (T7) o WER final e a intenção continuarem fora das metas, o resultado vai ao Sponsor como decisão, com a distância medida e as opções, sem baixar as metas.
