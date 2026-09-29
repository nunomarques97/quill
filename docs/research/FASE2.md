# Quill: medições da Fase 2 com a voz real

**Estado: baseline medida a 2026-09-28** (tarefa T2); **transcrição por blocos medida a 2026-09-29** (tarefa T3, etapa `streamed`); **juiz de intenção revisto e limpeza do texto medida a 2026-09-29** (tarefa T4, etapa `cleanup`); **vocabulário pessoal medido a 2026-09-29** (tarefa T5, etapa `vocabulary`); **aprendizagem de correções medida a 2026-09-29** (tarefa T6, etapa `corrections`); **perfis da janela ativa medidos a 2026-09-29** (tarefa T7, etapa `profiles`). Motor decidido pelo Sponsor: Whisper local (faster-whisper, float16, CUDA, RTX 5060 Ti) com vocabulário (`initial_prompt` e `hotwords`), 0 EUR/mês; o áudio não sai do PC. A baseline (`raw`) é do large-v3. Desde a decisão do Sponsor de 2026-09-29, o modelo por omissão da aplicação é o large-v3-turbo, porque só ele cumpre a meta de latência; o large-v3 fica como modo preciso (ver [Transcrição por blocos](#transcrição-por-blocos-etapa-streamed)). A etapa `profiles` é o texto final da aplicação.

Este documento contém só números agregados: nenhum texto falado, nenhum nome de projeto real e nenhum caminho da máquina. Os resultados por frase ficam apenas em `bench/results/`, que o Git ignora. Os números vêm de [phase2-summary.json](phase2-summary.json); o harness está em [bench/](../../bench/README.md#pipeline-evaluation).

## Resumo

- **Ditado (36 gravações novas), baseline large-v3:** WER 13,5 % contra a referência limpa e 16,9 % contra a literal; erro em termos ingleses 2,3 %; erro em nomes de projeto 0,0 %; intenção preservada 47,2 % (com o juiz revisto na T4); o próprio Whisper já tira 64,3 % das hesitações e repetições; faltam 19 de 960 palavras de conteúdo.
- **Comandos (44 gravações da Fase 1), baseline large-v3:** WER 28,3 %, termos 23,5 %, nomes 21,1 %: exatamente os valores da Fase 1 para large-v3 com vocabulário, o que confirma que o harness reproduz a medição. A intenção era 52,3 % com o juiz da Fase 1 e é 45,5 % com o juiz revisto.
- **Juiz de intenção revisto (T4):** comparado com uma revisão manual das 80 frases da etapa `streamed`, o juiz antigo concordava em 67 (83,8 %) e era demasiado brando; o juiz revisto concorda em 71 (88,8 %). Ver [Revisão do juiz de intenção](#revisão-do-juiz-de-intenção).
- **Limpeza (T4, etapa `cleanup`):** as regras determinísticas removem 96,4 % das hesitações e repetições (meta ≥ 95 %) sem apagar nenhuma palavra de conteúdo (meta 0) e baixam o WER limpo do ditado de 13,0 % para 11,0 %. O `qwen3:8b` fica desligado: apagava 13 palavras de conteúdo e o p95 por frase era 0,97 s. Ver [Limpeza do texto](#limpeza-do-texto-etapa-cleanup).
- **Vocabulário pessoal (T5, etapa `vocabulary`):** o vocabulário pessoal entra nas dicas do Whisper com prioridade e um corretor pós-reconhecimento acerta a grafia de nomes e termos quase certos. No ditado, o erro em nomes volta a 0,0 % (era 11,1 % com o turbo), os termos ficam em 2,3 %, o WER limpo desce para 10,6 %, a intenção sobe para 58,3 % e as hesitações removidas ficam em 96,4 %. Nos comandos, os nomes descem para 15,8 % e a intenção sobe para 36,4 %, mas os termos ingleses ficam em 47,1 %. Ver [Vocabulário pessoal](#vocabulário-pessoal-etapa-vocabulary).
- **Decisão do Sponsor (2026-09-29), depois da T5:** o large-v3-turbo continua a ser o motor por omissão. As metas de nomes e termos (≤ 10 %) aplicam-se ao conjunto de ditado, que é o uso real do produto, e estão cumpridas; o conjunto curto de comandos passa a ser um indicador reportado, não uma meta que bloqueia. Ver [Decisão sobre o conjunto de comandos](#decisão-sobre-o-conjunto-de-comandos).
- **Correções aprendidas (T6, etapa `corrections`):** simulando, pela ordem fixa das gravações, um utilizador que corrige cada texto, com a regra do produto (aplica sozinha uma troca vista em dois ditados), todas as repetições de erros já aprendidos foram corrigidas (18 de 18 nos comandos e 1 de 1 no ditado) e nenhuma palavra certa passou a errada (meta 0). O WER dos comandos desce de 29,7 % para 24,7 % e o do ditado de 10,6 % para 10,5 %; termos, nomes e intenção não mudam. Ver [Correções aprendidas](#correções-aprendidas-etapa-corrections).
- **Metas cumpridas no ditado:** erro em termos e em nomes ≤ 10 %; hesitações e repetições removidas ≥ 95 % e 0 palavras de conteúdo apagadas pela limpeza; correções aprendidas (100 % das repetições corrigidas, 0 erros novos), também nos comandos.
- **Perfis da janela ativa (T7, etapa `profiles`):** cada ditado recebe as regras do perfil da janela de destino (Claude Code e VS Code técnicos, WhatsApp informal, email com frases completas). As regras só mudam pontuação e maiúsculas, por isso nenhuma métrica muda: mudaram 9 dos 36 ditados (os do WhatsApp perdem o ponto final) e nenhum comando. Ver [Perfis da janela ativa](#perfis-da-janela-ativa-etapa-profiles).
- **Metas por cumprir, no texto final (etapa `profiles`):** WER final ≤ 10 % (ditado 10,5 %, comandos 24,7 %) e intenção ≥ 95 % (ditado 58,3 %, comandos 36,4 %). Foram ao Sponsor como decisão, com a distância e as opções. **Decisão do Sponsor (2026-09-29), opção B:** acabar agora a aplicação e voltar a medir depois de algumas semanas de uso real; as metas não baixam e esta distância fica registada como aberta. Ver [Decisão do Sponsor: WER final e intenção](#decisão-do-sponsor-wer-final-e-intenção). A digitação sem perdas ainda não tem medição (tarefa T11).
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
| comandos | vocabulary | 44 | 29,7 % | 29,7 % | 47,1 % | 15,8 % | 36,4 % | — | — | — |
| comandos | corrections | 44 | 24,7 % | 24,7 % | 47,1 % | 15,8 % | 36,4 % | — | — | — |
| comandos | profiles | 44 | 24,7 % | 24,7 % | 47,1 % | 15,8 % | 36,4 % | — | — | — |
| ditado | raw | 36 | 16,9 % | 13,5 % | 2,3 % | 0,0 % | 47,2 % | 64,3 % | 19 | — |
| ditado | streamed | 36 | 16,5 % | 13,0 % | 2,3 % | 11,1 % | 52,8 % | 64,3 % | 19 | — |
| ditado | cleanup | 36 | 17,8 % | 11,0 % | 2,3 % | 11,1 % | 55,6 % | 96,4 % | 20 | 0 |
| ditado | vocabulary | 36 | 17,4 % | 10,6 % | 2,3 % | 0,0 % | 58,3 % | 96,4 % | 19 | — |
| ditado | corrections | 36 | 17,3 % | 10,5 % | 2,3 % | 0,0 % | 58,3 % | 96,4 % | 19 | — |
| ditado | profiles | 36 | 17,3 % | 10,5 % | 2,3 % | 0,0 % | 58,3 % | 96,4 % | 19 | — |

| Conjunto | Erros já aprendidos que se repetem | Corrigidos | Taxa corrigida | Repetições antes de ativar | Erros novos | Substituições aplicadas | Ativas / pendentes / em conflito no fim |
|---|---|---|---|---|---|---|---|
| comandos | 18 | 18 | 100,0 % | 4 | 0 | 18 | 3 / 31 / 4 |
| ditado | 1 | 1 | 100,0 % | 2 | 0 | 1 | 1 / 49 / 0 |
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

A limpeza corre depois da transcrição e antes da digitação (`quill/cleanup.py`). Por omissão são regras determinísticas, genéricas para português ditado e nunca escritas a partir do guião: tiram hesitações (`hum`, `hã`, `é pá`; `pronto` no início de uma oração ou a fechar uma frase, mas não em «está pronto», «tenho o relatório pronto» ou «pronto para»; `tipo` só quando não é nome, como em «o tipo», «por tipo», «sem tipo», «tipo de» ou no fim de uma frase), tiram repetições imediatas de 1 a 4 palavras (exceto números, palavras de ênfase como «não, não» e pares válidos como «para para» ou «se se»), arrumam a pontuação e os espaços e põem maiúscula no início das frases. Termos ingleses, nomes e números nunca são alterados. Aplicar a limpeza duas vezes dá o mesmo texto.

A etapa `cleanup` aplica estas regras ao texto da etapa `streamed` (tabela acima). A regra do `pronto` a fechar uma frase foi acrescentada na T5 (opção A da decisão do Sponsor); não muda nenhum texto da etapa `cleanup` e só muda uma frase da etapa `vocabulary`.

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

## Vocabulário pessoal (etapa `vocabulary`)

O vocabulário pessoal fica em `local/vocabulary.toml`, que o Git ignora; o formato está em [vocabulary.example.toml](../../vocabulary.example.toml), só com entradas inventadas, e `py -3.12 -m quill.vocabulary --check` valida o ficheiro e mostra só contagens. Tem nomes de projeto, termos técnicos e, em `[variants]`, formas faladas ou mal ouvidas de uma entrada. O código está em `quill/vocabulary.py` e é o mesmo que a aplicação usa.

- **Dicas do Whisper, por esta ordem:** nomes pessoais, nomes de projeto resolvidos (só no harness), termos pessoais e a lista genérica `bench/terms_en.txt`. Os repetidos saem (sem distinguir maiúsculas) e a lista é cortada pelo fim quando passa o limite do prompt (600 caracteres), por isso os nomes são os últimos a sair. As variantes nunca vão nas dicas. Na medição couberam as 41 dicas e nenhuma foi cortada.
- **Corretor depois do reconhecimento:** depois da limpeza, uma palavra ou um grupo de palavras muito parecido com uma entrada passa a ter a grafia da entrada. A comparação ignora acentos, maiúsculas, hífenes e espaços e aceita no máximo 1 letra diferente em entradas de 7 ou mais letras e 2 em entradas de 12 ou mais, sempre com a mesma primeira letra. Entradas curtas só mudam quando são ouvidas exatamente ou como variante declarada; plurais e palavras comuns parecidas (por exemplo «pronto» perto de «prompt») ficam como estão; se duas entradas estiverem à mesma distância, nada muda. O corretor usa só a lista do vocabulário, nunca o texto de referência.
- **Como se mediu:** o `local/vocabulary.toml` usado tem os 6 nomes de projeto do projeto de referência, sem termos nem variantes. Não se acrescentaram variantes tiradas das próprias gravações, porque isso seria medir com as respostas. Como as dicas mudam (mais nomes, à frente), a etapa volta a transcrever por blocos e a limpar cada gravação com as dicas do produto antes do corretor; as etapas anteriores ficam com as dicas da baseline, para continuarem comparáveis. As etapas `raw`, `streamed` e `cleanup` deram exatamente os mesmos valores da T4.

Leitura dos resultados (tabela acima, etapa `vocabulary` contra `cleanup`):

- **Ditado: melhor ou igual em tudo.** Nomes 11,1 % → 0,0 %; WER limpo 11,0 % → 10,6 %; intenção 55,6 % → 58,3 %; termos iguais (2,3 %); hesitações removidas iguais (96,4 %); palavras apagadas 20 → 19. O corretor mudou 2 trechos. Com as novas dicas, o Whisper escreveu uma hesitação a fechar uma frase, sem vírgula antes; a primeira medição deu por isso 94,6 %, e a regra genérica do `pronto` a fechar uma frase (ver [Limpeza do texto](#limpeza-do-texto-etapa-cleanup)) repõe os 96,4 %.
- **Comandos: nomes e intenção melhoram, termos não.** Nomes 18,4 % → 15,8 %; intenção 29,5 % → 36,4 %; WER 30,0 % → 29,7 %; termos 47,1 %, iguais. O corretor mudou 1 trecho.
- **Porque não chega nos comandos.** Faltam 8 dos 17 termos e 6 dos 38 nomes, e quase todos estão longe demais para uma correção segura: 5 dos 8 termos são o verbo inglês «run» ouvido como palavras portuguesas diferentes; outro é «prompt» ouvido como «pronto», uma palavra portuguesa comum que o corretor não pode mudar sem estragar o ditado. Nos nomes, 4 das 6 falhas são um nome transcrito como palavras portuguesas ou cortado e 2 são o nome do assistente trocado ou em falta. Aceitar mais letras de distância mudaria palavras comuns.
- **Só o corretor, sem mudar as dicas (para comparação):** comandos WER 29,7 %, termos 47,1 %, nomes 15,8 %, intenção 29,5 %; ditado WER limpo 10,8 %, nomes 0,0 %, intenção 58,3 %, hesitações removidas 96,4 %. As dicas pessoais valem a intenção dos comandos (+6,8 pp) e, com a regra do `pronto` a fechar uma frase, já não custam nenhum trecho de hesitação no ditado.
- **Rede de segurança (decisão do Sponsor, T3).** O erro em nomes (15,8 %) e em termos ingleses (47,1 %) nos comandos continuou acima de 10 % com o turbo e foi ao Sponsor como decisão. O modo preciso (large-v3) também não cumpre nos comandos (termos 23,5 %, nomes 15,8 %, medidos sem o corretor) e falha a meta de latência.

### Decisão sobre o conjunto de comandos

**Decisão do Sponsor (2026-09-29), opção A.** O large-v3-turbo continua a ser o motor por omissão, com as dicas pessoais e o corretor. As metas de erro em nomes de projeto e em termos ingleses (≤ 10 %) aplicam-se ao conjunto real de ditado, que é a forma como o produto é usado, e aí estão cumpridas (nomes 0,0 %, termos 2,3 %). O conjunto curto de comandos continua a ser medido e reportado como indicador, mas deixa de bloquear: o large-v3 também falha nele (nomes 21,1 %, termos 23,5 %). Espera-se que a aprendizagem de correções (T6) reduza a distância nos comandos, porque as trocas que se repetem (como o verbo «run») passam a ser corrigidas à segunda vez. As metas do ditado não mudam.

Na prática, `--require vocabulary` verifica nomes e termos só no ditado e mostra os valores dos comandos como `info`, sem falhar. As outras metas (WER final, intenção) continuam a aplicar-se aos dois conjuntos.

## Correções aprendidas (etapa `corrections`)

A aplicação aprende com as correções do Sponsor. O código está em `quill/corrections.py`, `quill/edits.py` e `quill/review.py`; as correções ficam em `local/corrections.json`, que o Git ignora.

- **Duas formas de corrigir.** (1) Tecla de correção (F16 por omissão, configurável em `[corrections] key`): com o texto corrigido selecionado, a tecla copia a seleção guardando antes o conteúdo da área de transferência e repondo-o no fim, também quando algo falha; se outro programa mudar a área de transferência entretanto, essa mudança fica. (2) Deteção de edições manuais: durante 30 s depois de o texto ser escrito, na mesma janela, a aplicação acompanha em memória as teclas que editam esse texto (letras, Backspace, Delete, setas, Home/End, Ctrl+Backspace). Um clique, colar, desfazer, mudar de janela, uma seleção ou uma tecla desconhecida fazem-na desistir em vez de adivinhar. As teclas nunca são guardadas nem registadas; só as trocas deduzidas.
- **Regra.** De cada correção tiram-se trocas de palavras ou grupos até 3 palavras (por exemplo uma palavra mal ouvida pela certa). Não se aprende nada de uma reescrita (mais de 5 trocas ou menos de metade das palavras iguais), de uma palavra só acrescentada ou só apagada, nem de uma troca só entre palavras gramaticais (artigos, preposições, pronomes, conjunções), porque a forma certa depende da frase. Uma troca vista em dois ditados diferentes passa a ativa e é aplicada sozinha ao texto seguinte, em palavras inteiras e com as maiúsculas do texto; o mesmo ditado duas vezes conta uma vez. Trocas em conflito (a mesma palavra com duas correções diferentes, ou uma troca e a inversa) nunca são aplicadas sozinhas.
- **Revisão semanal.** `py -3.12 -m quill.review` mostra as correções ativas e as pendentes e pergunta, uma a uma, se aprova, elimina ou mantém; uma correção eliminada não volta a ser aprendida e uma aprovada fica ativa mesmo que só tenha sido vista uma vez. `--check` mostra o lembrete quando passaram 7 dias desde a última revisão.
- **Como se mediu.** Simulação pela ordem fixa das gravações de cada conjunto, sobre o texto da etapa `vocabulary`: cada gravação recebe primeiro as trocas ativas até ali; depois o utilizador simulado corrige o texto para a referência limpa e o produto aprende com essa correção, com a mesma regra da aplicação. Cada gravação é um ditado. Uma **repetição** é um erro numa gravação cuja troca já estava ativa; um **erro novo** é uma palavra que a gravação tinha certa e que as trocas aplicadas estragaram. Diferenças só de maiúsculas ou de números por extenso não contam como erros, porque o WER também as ignora.

Leitura dos resultados (tabela acima, etapa `corrections` contra `vocabulary`):

- **Metas cumpridas nos dois conjuntos.** Comandos: 18 repetições, 18 corrigidas, 0 erros novos. Ditado: 1 repetição, 1 corrigida, 0 erros novos. As etapas anteriores deram exatamente os mesmos valores da T5.
- **Comandos: o WER desce 5 pontos, os termos e os nomes não.** WER 29,7 % → 24,7 %. As 18 correções são todas a mesma saudação ouvida de duas formas diferentes, que se repete em muitas gravações. O verbo inglês «run», a principal falha de termos, é ouvido como palavras portuguesas diferentes de cada vez, e quando a mesma palavra se repete a correção não é sempre igual (fica em conflito); por isso nunca chega a ser aplicado sozinho e os termos ficam em 47,1 %. Os nomes que falham também não se repetem da mesma forma depois de aprendidos.
- **Ditado: pouco para aprender.** WER limpo 10,6 % → 10,5 %. Dos 62 erros, 57 são diferentes entre si e só 3 aparecem mais do que uma vez: frases variadas quase não repetem o mesmo erro. A aprendizagem ajuda sobretudo nos erros de todos os dias que se repetem (palavras e nomes que o Whisper ouve sempre mal); não resolve a distância à meta de WER, que é trabalho da T7.
- **Palavras gramaticais.** Uma medição de ensaio, feita por engano sem o vocabulário pessoal (texto do ditado um pouco diferente) e ainda sem a regra das palavras gramaticais, deu 1 erro novo no ditado: a troca de uma preposição contraída do plural para o singular, aprendida em duas frases, estragou uma terceira frase onde a forma original estava certa. Com a regra, esse erro desaparece nesse texto. No texto final, com ou sem a regra, os resultados das repetições e dos erros novos são os mesmos; a regra só evita que trocas deste tipo cheguem a ficar ativas (mais tarde, no produto).
- **Risco que a revisão cobre.** Uma troca ativa de uma palavra comum (como a saudação dos comandos) muda essa palavra em todo o texto seguinte. Nas 80 gravações não estragou nenhuma palavra certa, mas a revisão semanal serve para eliminar uma troca que não se queira.

## Perfis da janela ativa (etapa `profiles`)

A aplicação escolhe um perfil pela janela em primeiro plano quando o gatilho é premido (`quill/profiles.py`): nome do processo, classe e título da janela, comparados com os perfis de `[profiles.*]` em `local/quill.toml` (formato em [quill.example.toml](../../quill.example.toml)), pela ordem do ficheiro. O primeiro perfil que corresponde ganha; uma janela que não corresponde a nenhum usa o perfil `default`. Um processo que o Windows não deixa ler (uma janela elevada) nunca corresponde a uma lista de processos: o perfil não é adivinhado só pelo título.

- **Claude Code:** processo do terminal ou do editor (`WindowsTerminal.exe` ou `Code.exe` no exemplo) e «Claude Code» no título. Vem antes do VS Code, porque o VS Code mostra esse título quando o separador do Claude Code está ativo. É este perfil que o gatilho de envio consulta antes de carregar em Enter.
- **Regras de cada perfil:** só mudam pontuação, espaços e a maiúscula no início das frases; nunca acrescentam, tiram ou trocam palavras, e aplicá-las duas vezes dá o mesmo texto. Claude Code e VS Code (técnico): termina sempre com sinal de fim de frase e as reticências finais passam a ponto. WhatsApp (informal): sem ponto final (ficam `?`, `!` e reticências). Email (frases completas): cada frase termina com sinal, as reticências finais passam a ponto e uma saudação inicial seguida de texto recebe a vírgula («Bom dia, …»). Outras janelas: a pontuação da limpeza.
- **Estilo de escrita:** exemplos de textos do Sponsor em `local/style/` (ignorada pelo Git; `<perfil>.txt` ou `default.txt`, exemplos separados por uma linha em branco) entram no prompt do modelo local só quando a limpeza está em modo `llm`, com a instrução do perfil; `py -3.12 -m quill.profiles --check` mostra só quantos exemplos há. Com a limpeza por regras, que é a opção por omissão desde a T4, os exemplos não são lidos e o estilo é o das regras do perfil. Por isso a etapa mede só as regras; não havia exemplos de estilo em `local/style/` nesta medição.
- **Como se mediu:** cada gravação do ditado recebe o perfil da coluna `estilo` do guião (9 por perfil); os comandos não têm estilo e recebem o perfil `default`. As etapas anteriores foram medidas outra vez e deram exatamente os mesmos valores da T6.

Leitura dos resultados (tabela acima, etapa `profiles` contra `corrections`):

- **Nenhuma métrica muda, como esperado.** O WER, os termos, os nomes, as hesitações e as palavras apagadas ignoram pontuação e maiúsculas. Mudaram 9 textos do ditado (os 9 do WhatsApp perdem o ponto final) e 0 dos comandos. O juiz de intenção viu os textos novos e não mudou nenhum veredicto: 21 de 36 no ditado e 16 de 44 nos comandos.
- **Por perfil, no ditado** (só contagens): WER limpo 9,1 % no Claude Code, 10,5 % no VS Code, 12,9 % no WhatsApp e 10,2 % no email; intenção preservada 5, 6, 4 e 6 em 9. As mensagens informais são as mais curtas e as que têm mais erros de reconhecimento por palavra.

## Distância às metas da Fase 2

Valores da etapa `profiles`, o texto final da aplicação (large-v3-turbo, limpeza por regras, vocabulário pessoal, correções aprendidas e perfis); entre parênteses, a baseline `raw` do large-v3. Intenção medida com o juiz revisto. As metas não baixam; cada linha diz que tarefa a trata.

| Meta | Comandos | Ditado | Distância | Tarefa |
|---|---|---|---|---|
| WER final (referência limpa) ≤ 10 % | 24,7 % (28,3 %) | 10,5 % (13,5 %) | comandos +14,7 pp (89 erros em 360 palavras; a meta pede no máximo 36); ditado +0,5 pp (101 erros em 960 palavras; a meta pede no máximo 96) | aberta: nova medição depois do uso real (decisão do Sponsor, T7) |
| Intenção preservada ≥ 95 % | 36,4 % (45,5 %) | 58,3 % (47,2 %) | comandos −58,6 pp (16 de 44; a meta pede 42); ditado −36,7 pp (21 de 36; a meta pede 35) | aberta: nova medição depois do uso real (decisão do Sponsor, T7) |
| Erro em nomes de projeto ≤ 10 % (meta no ditado; comandos como indicador) | 15,8 % (21,1 %) | 0,0 % (0,0 %) | ditado cumprida; comandos +5,8 pp (6 nomes em 38), só indicador | T5; a T6 não mudou os comandos |
| Erro em termos ingleses ≤ 10 % (meta no ditado; comandos como indicador) | 47,1 % (23,5 %) | 2,3 % (2,3 %) | ditado cumprida; comandos +37,1 pp (8 termos em 17), só indicador | T5; a T6 não mudou os comandos |
| Hesitações e repetições removidas ≥ 95 % | não se aplica | 96,4 % (64,3 %) | cumprida | T4 e T5 |
| Palavras de conteúdo apagadas pela limpeza = 0 | não se aplica | 0 | cumprida na etapa `cleanup`; as 19 omissões do reconhecimento continuam visíveis | T4 |
| Latência p95 ≤ 0,5 s do largar do gatilho ao texto final (até 15 s; meta do Sponsor) | cumprida | cumprida | cumprida com o large-v3-turbo nos dois conjuntos juntos, ao ritmo real e com a GPU livre; com o large-v3 (modo preciso) não; os tempos ficam só em `bench/results/` | T3 |
| 0 caracteres perdidos ou duplicados na digitação | não medido | não medido | — | T11 |
| Correções aprendidas: 100 % das repetições corrigidas, 0 erros novos | 18 de 18, 0 novos | 1 de 1, 0 novos | cumprida | T6 |

A meta de intenção é a mais distante nos dois conjuntos. O juiz foi revisto à mão na T4 e era brando, não exigente: com o juiz revisto a distância aumentou, e a revisão manual dá valores ainda mais baixos (25,0 % nos comandos e 47,2 % no ditado, na etapa `streamed`). A distância vem sobretudo de erros de reconhecimento (palavras e nomes trocados), que a limpeza não corrige. No fim das camadas de adaptação (T7), o WER final e a intenção continuam fora das metas nos dois conjuntos; o resultado foi ao Sponsor como decisão, sem baixar as metas, e o Sponsor escolheu voltar a medir depois do uso real (ver a secção seguinte).

## Decisão do Sponsor: WER final e intenção

**Distância medida no texto final (etapa `profiles`).**

- **Ditado:** WER 10,5 %: 101 erros em 960 palavras; para chegar a 10 % são precisos 5 erros a menos. Intenção 58,3 %: 21 de 36 frases; a meta pede 35. Das 15 frases falhadas, 14 foram rejeitadas pelo juiz e 1 pela regra de nomes e termos. Só 3 das 36 frases saem sem nenhum erro de palavras e 11 têm exatamente um erro.
- **Comandos:** WER 24,7 %: 89 erros em 360 palavras; a meta pede no máximo 36. Intenção 36,4 %: 16 de 44; a meta pede 42. Das 28 falhadas, 12 falham a regra de nomes e termos, 4 estão cortadas no fim e 12 foram rejeitadas pelo juiz.
- **Causa:** erros de reconhecimento (palavras e nomes ouvidos de outra forma). A limpeza, o vocabulário, as correções e os perfis já não apagam nem trocam palavras certas; o que falta é o Whisper ouvir melhor a voz do Sponsor. Numa frase de 20 palavras, um WER de 10 % são 2 palavras erradas, e um só erro basta muitas vezes para mudar a intenção; por isso uma intenção de 95 % pede muito menos erros do que o WER de 10 %.

**O que já foi medido e não resolve** (não são opções):

- Modo preciso (large-v3): no ditado deu WER limpo 13,8 % contra 13,0 % do turbo na etapa `streamed`, e cerca de 1,1 s do largar ao texto final (falha a meta de 0,5 s).
- Limpeza com o `qwen3:8b`: mais 5,5 pp de intenção no ditado, mas apaga 13 palavras de conteúdo (meta 0) e soma 0,97 s no p95.
- Motores cloud (Fase 1): nenhum dos medidos foi melhor do que o large-v3 local, e o Sponsor decidiu 0 EUR e o áudio sempre no PC.

**Opções apresentadas ao Sponsor:**

- **A. Afinar o Whisper à voz do Sponsor, no PC (0 EUR).** Treinar o large-v3-turbo com gravações novas do Sponsor (nunca as dos conjuntos de medida, que ficam só para avaliar). É a única opção que ataca a causa. Custos: instalar pacotes de treino no `.venv` (os comandos exatos vão num pedido de aprovação próprio), gravar 1 a 2 horas de leitura e algumas horas de GPU. O ganho não está medido: decide-se com um ensaio pequeno e um critério de continuar ou parar medido nestes dois conjuntos.
- **B. Manter o motor e medir em uso real.** O vocabulário pessoal e as correções aprendidas crescem com o uso; voltar a medir daqui a algumas semanas, com gravações novas. As metas de WER e intenção continuam por cumprir até lá.
- **C. O Sponsor redefine onde as metas se aplicam.** Por exemplo, tratar os comandos curtos também como indicador para o WER e a intenção, como já decidiu para nomes e termos a 2026-09-29. Mesmo assim, o ditado continua fora das metas (WER 10,5 % e intenção 58,3 %). Só o Sponsor pode mudar metas; este documento não as baixa.

A recomendação foi A, como ensaio com critério de paragem, com B em paralelo.

**Decisão do Sponsor (2026-09-29): opção B.** Acabar agora a aplicação (indicador, aplicação, modo comando e aceitação) e voltar a medir depois de algumas semanas de uso real, com o vocabulário e as correções que o uso diário for criando. Essas correções passam a ser o material de um ensaio posterior de afinação local (opção A), sem sessões de gravação extra. Nada é instalado agora. As metas não baixam: o WER final ≤ 10 % e a intenção ≥ 95 % continuam por cumprir nos dois conjuntos e esta distância fica registada como aberta até à nova medição.
