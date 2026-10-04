# Quill: relatório da Fase 12 (a última mensagem do Claude como contexto do botão 5)

**Estado: concluída a 2026-10-04, sem o Sponsor presente; falta gravar as respostas ao Claude.** Este documento tem só números agregados: nenhum texto falado, nenhuma mensagem do Claude, nenhum nome de projeto, nenhum termo real e nenhum caminho da máquina. O texto de cada gravação fica em `bench/results/prompts/20261004-014332/`, pasta ignorada pelo Git. Tudo correu no PC (o Whisper large-v3-turbo que já estava no disco e o Ollama partilhado, com o qwen3:8b), sem instalar nem descarregar nada, sem tirar nem apagar modelos de outros projetos e com custo 0 €.

## Resultado

- **Novo no botão 5 para o Claude Code:** com o projeto reconhecido, o Quill lê a última mensagem do Claude nessa conversa, tira dela uma lista curta de palavras e usa-a de três formas: como dicas do Whisper (só depois de ouvir algo parecido), como palavras que a correção pode usar para trocar uma palavra mal ouvida que soe parecido, e para não organizar em «Objetivo»/«Pedido» uma resposta curta a uma pergunta ou a opções. **Nunca copia a mensagem do Claude para o seu texto.** Fica **ligada** por omissão (`[claude_code] last_reply_context = true`); desligada, o botão 5 é exatamente o de antes.
- **Sem regressão nas gravações que existem:** nos 15 prompts e nos 9 ditados para o Claude Code, com a definição ligada, os termos do domínio ficaram em **2 de 16** (meta: no máximo 3), **0 palavras perdidas**, **0 inventadas** e 0 palavras do projeto fora do «Contexto». Do largar ao texto, até 3,253 s nos prompts e 4,202 s nos ditados (meta: no máximo 6 s). Os prompts ficaram com **66 erros de palavras em 304 (21,71 %)**, contra 68 (22,37 %) na Fase 11.
- **A meta de 12 % nos prompts continua por cumprir** (seriam no máximo 36 erros). A descida de 68 para 66 não vem desta fase: nenhuma destas gravações tem mensagem do Claude emparelhada, por isso o botão 5 correu sem contexto da resposta, pelo mesmo caminho de antes. A diferença é a variação normal entre duas medições (a transcrição sozinha passou de 71 para 70 erros).
- **Ainda não há medição com mensagens do Claude:** as gravações que existem não respondem a nenhuma mensagem do Claude. A medição com mensagens emparelhadas espera pelas 10 respostas a gravar (passos em «Decisões para o Sponsor» e em [GRAVAR-RESPOSTAS.md](GRAVAR-RESPOSTAS.md)).

## O que mudou (T1 a T6)

| Tarefa | O que mudou |
|---|---|
| T1: encontrar e ler a última mensagem | Novo `quill/claude_reply.py`. O aviso «Claude acabou» (o hook `Stop` que já estava instalado, sem ser preciso instalar outra vez) passa a deixar, só para as conversas em que está a trabalhar, uma nota pequena por projeto em `local\claude-pointers` (pasta ignorada): o identificador da conversa, o ficheiro dela e a hora. O Quill usa essa nota para abrir a conversa certa; sem nota, usa a conversa mais recente da pasta do projeto. Lê só o fim do ficheiro e só o texto que o Claude mostrou. |
| T2: tirar as palavras | Novo `quill/reply_terms.py`: do texto visível tira as opções oferecidas (numeradas, com letras ou com nome), identificadores e nomes de ficheiros do código, e palavras com conteúdo em português e inglês, no máximo 40. Deita fora números, palavras com algarismos, «sim», «não», «se», «ou» e semelhantes, palavras curtas e hesitações. Também diz se a mensagem termina com uma pergunta ou com opções. |
| T3: correção e enriquecimento | A correção recebe as palavras como dados (nunca como instruções) e só pode usar uma delas para trocar uma palavra mal ouvida que soe parecido. A verificação da correção nunca deixa acrescentar uma palavra da mensagem e nunca a usa para nomes, números, negações nem condições. Se a mensagem do Claude pergunta ou oferece opções e a resposta tem até 25 palavras, o texto corrigido é enviado sem ser organizado; as palavras do Claude nunca entram no enriquecimento, por isso a verificação dele continua a recusá-las como inventadas. |
| T4: ligação ao botão 5 | Só o botão 5 para o Claude Code, com o projeto reconhecido, procura a mensagem, uma vez por ditado, ao mesmo tempo que as dicas do projeto. Ao largar, espera no máximo 0,3 s por uma procura ainda a correr. `python -m quill --check` diz se a definição está ligada e se a pasta das conversas do Claude Code se pode ler, e `python -m quill.claude_reply --dry-run` mostra o que cada projeto conhecido daria (só números). |
| T5: medição e guião | `bench.prompts` emparelha cada gravação com um ficheiro `local\replies\<gravação>.md` (pasta ignorada) e mede-a com e sem a mensagem; nunca lê as conversas do Claude Code. Novo conjunto de gravação `replies` (`rr-01` a `rr-10`) com o guião `bench/dictation/guiao-respostas-pt.md` (só marcadores e descrições genéricas) e os passos em [GRAVAR-RESPOSTAS.md](GRAVAR-RESPOSTAS.md). |
| T6: medição e documentação | A medição em baixo, este documento, o [README](../../README.md) e o [guia de uso](../USAR.md). O resumo só com números está em [replies-summary.json](replies-summary.json); o da Fase 11, [prompts-summary.json](prompts-summary.json), ficou como estava. |

## Limites de privacidade

- **Só leitura, só no PC:** o Quill nunca escreve, bloqueia nem apaga nada na pasta de definições do Claude Code e nada sai do PC. As palavras da mensagem só vão para o Whisper e para o Ollama locais.
- **Só ficheiros seguros:** só abre ficheiros normais dentro da pasta `projects` das definições do Claude Code. Recusa atalhos, junções e outros pontos de reanálise, `..`, caminhos de rede e de dispositivo, e notas que apontem para fora dessa pasta. Confirma que o ficheiro aberto é o mesmo que verificou.
- **Com limites:** número de ficheiros vistos na pasta, bytes lidos (só o fim do ficheiro), tamanho de cada linha e tempo, todos com teto. Usa no máximo 4000 caracteres do fim da mensagem e 40 palavras (`last_reply_max_chars`, `last_reply_max_terms`). Uma nota ou conversa com mais de 12 horas (`last_reply_max_age_h`) não conta.
- **Só o texto visível:** chamadas de ferramentas, resultados de ferramentas, pensamento do modelo e conversas de subagentes ficam de fora; do código só saem identificadores e nomes de ficheiros.
- **Só conversas em que está a trabalhar, pela nota:** a nota só é deixada quando o Claude Code marca a conversa como acompanhada (`CLAUDE_CODE_SESSION_ATTENDED` igual a 1); as execuções que ele marca como automáticas, como `claude -p`, não deixam nota.
- **O limite da alternativa:** sem nota (por exemplo, numa conversa que ainda não terminou nenhuma resposta desde esta versão), o Quill usa a conversa mais recente da pasta do projeto. Se no mesmo projeto estiver a correr uma execução automática, essa pode ser a mais recente, e então as palavras vêm dela. O efeito máximo é a correção poder trocar uma palavra mal ouvida por uma palavra dessa execução que soe parecido; nunca acrescenta palavras.
- **Nada no registo:** o registo e o `--dry-run` só dão motivos, origem (`pointer` ou `newest`), contagens e milissegundos. Nunca o texto, o título, o identificador da conversa nem um caminho. O resumo da medição passou pela verificação de privacidade de `bench.prompts`.

## Números medidos com a sua voz

Comando: `.venv\Scripts\python -m bench.prompts --summary docs/research/replies-summary.json`, com as definições da aplicação (`last_reply_context` ligada, a passagem final desligada), o large-v3-turbo e o qwen3:8b no Ollama partilhado, nas gravações que já existiam: os 15 prompts e os 9 ditados para o Claude Code. Nenhuma gravação tem mensagem do Claude emparelhada (0 de 24), por isso nenhuma usou contexto da resposta: em todas, a procura demorou 0 ms e o enriquecimento nunca foi saltado como resposta. A Fase 11 é a medição de [prompts-summary.json](prompts-summary.json).

| Medição | Fase 11 | Fase 12 | Meta |
|---|---|---|---|
| Termos do domínio errados, prompts (botão 5 completo; depois da limpeza) | 2 de 16; 4 | **2 de 16**; 4 | no máximo 3 |
| Palavras perdidas / inventadas (prompts e ditados) | 0 / 0 | **0 / 0** | 0 / 0 |
| Palavras do projeto fora do «Contexto» | 0 | 0 | 0 |
| Erros de palavras, prompts (transcrição / limpeza / botão 5 completo) | 71 / 71 / 68 de 304 (22,37 %) | 70 / 70 / **66 de 304 (21,71 %)** | no máximo 12 % (**não cumprida**) |
| Erros de palavras, ditados (transcrição / limpeza / botão 5 completo) | 31 / 25 / 25 de 253 (9,88 %) | 31 / 25 / 25 de 253 (9,88 %) | — |
| Nomes em falta, prompts (de 15) / ditados (de 4) | 5 / 0 | 4 / 0 | — |
| Enriquecimento pedido / aceite, prompts | 11 / 9 | 12 / 10 | — |
| Enriquecimento pedido / aceite, ditados | 7 / 6 | 7 / 6 | — |
| Chamadas ao modelo acima do tempo da aplicação | 1 de 30 (prompts) | 0 de 30 (prompts), 0 de 18 (ditados) | — |

**Tempo de cada etapa, em segundos (a meio da lista / em 95 % dos casos):**

| Etapa | Prompts, Fase 11 | Prompts, Fase 12 | Ditados, Fase 11 | Ditados, Fase 12 |
|---|---|---|---|---|
| Transcrição (texto do streaming) | 0,24 / 0,294 | 0,232 / 0,276 | 0,211 / 0,324 | 0,244 / 0,332 |
| Pacote do projeto | 0,05 / 0,355 | 0,082 / 0,295 | 0,042 / 0,065 | 0,108 / 0,313 |
| Limpeza | 0,002 / 0,002 | 0,002 / 0,003 | 0,002 / 0,004 | 0,002 / 0,003 |
| Correção | 0,762 / 0,88 | 0,752 / 1,213 | 0,718 / 1,163 | 0,933 / 1,949 |
| Enriquecimento | 1,503 / 1,884 | 1,537 / 1,847 | 0,986 / 1,686 | 1,062 / 2,025 |
| **Do largar ao texto** | 2,292 / 2,916 | **2,455 / 3,253** | 1,308 / 2,784 | **1,646 / 4,202** |

Os dois conjuntos ficam dentro da meta de 6 s. Os ditados demoraram mais em 95 % dos casos (4,202 s contra 2,784 s): com só 9 ditados, esse número é o do ditado mais lento, e nesse ditado as duas chamadas ao modelo demoraram 1,949 s e 1,844 s. Como nenhum ditado usou a mensagem do Claude, o caminho foi o mesmo de antes; a diferença é do tempo de resposta do modelo nesta medição.

**O conjunto das respostas:** 10 linhas no guião, 0 gravadas, 10 pares por fazer. Aparece no resumo como `pending_recordings` e, sem gravações, com `reply context off`.

**A procura no seu PC** (`.venv\Scripts\python -m quill.claude_reply --dry-run`, só números, sem ditar): 34 pastas de projeto conhecidas. Em 14 há mensagem utilizável (7 pela nota do aviso, 7 pela conversa mais recente), com 2 a 40 palavras e 0 opções; 15 só têm conversas com mais de 12 horas (`stale`) e 5 não têm conversas do Claude Code (`no_session_dir`). Cada procura demorou no máximo 156 ms (16 ms a meio da lista nas 14 com mensagem), abaixo dos 0,3 s que o botão 5 espera ao largar; a procura corre enquanto ainda está a falar.

## Como verificar

1. No Terminal do Windows, na pasta do Quill, corra `.venv\Scripts\python -m bench.prompts --check docs\research\replies-summary.json`. Deve dizer `met` em todas as linhas, menos na dos 12 %, que diz `NOT met: prompts WER after the full mouse 5 pipeline: 21.7% (66 of 304 words ...)`, e no fim `pending pairs (replies): 10`.
2. Corra `.venv\Scripts\python -m quill --check`: a linha `last reply context` deve dizer `on; Claude Code projects folder readable, N project folders`. Corra também `.venv\Scripts\python -m quill.claude_reply --dry-run`: mostra cada projeto por número, com o motivo e as contagens, nunca o texto.
3. Ligue o Quill como de costume (`.venv\Scripts\python -m quill`), espere que o Claude Code termine uma resposta num projeto conhecido e responda-lhe com o botão 5. Em `local\logs\quill.log` aparece `last reply ok (source pointer, … terms, … options, … ms)` e, se a correção recebeu palavras, a linha `autorewrite:` diz `… reply terms`. Se a mensagem do Claude terminou com uma pergunta e respondeu em poucas palavras, o indicador mostra «Resposta ao Claude; foi o texto corrigido, sem enriquecer».

## Decisões para o Sponsor

1. **Gravar as 10 respostas ao Claude.** Sem elas não há números com a mensagem do Claude. Recomendação: gravar quando tiver 20 a 30 minutos. Custo 0 €, nada a instalar. Os passos completos estão em [GRAVAR-RESPOSTAS.md](GRAVAR-RESPOSTAS.md); em resumo:
   1. Abra o VS Code na pasta do Quill e abra um terminal (**Terminal → New Terminal**).
   2. Para cada linha `rr-01` a `rr-10` do guião `bench/dictation/guiao-respostas-pt.md`, procure numa conversa sua com o Claude Code uma mensagem dele como a descrita e copie só o texto visível.
   3. Guarde cada mensagem em `local\replies\rr-01.md` a `local\replies\rr-10.md` (crie a pasta se não existir).
   4. Em `local\bench.toml`, acrescente as secções `[replies.projects]` (2 projetos) e `[replies.terms]` (8 termos), como mostra o guia.
   5. Confirme com `py -3.12 -m bench.record --set replies --dry-run` (não liga o microfone): deve mostrar 10 pendentes, `2 of 2 projects and 8 of 8 terms named` e `reply files: 10 of 10 saved`.
   6. Ligue o headset, confirme que o microfone não está em silêncio e escolha um sítio sossegado.
   7. Grave com `py -3.12 -m bench.record --set replies`.
   8. Em cada resposta, leia-a em silêncio, carregue em Enter, diga-a como a diria ao Claude Code e carregue em Enter para parar.
   9. Se aparecer «Take rejeitado», grave outra vez; com «Guardado», Enter passa à seguinte (`r` repete, `q` faz pausa).
   10. No fim, confirme com o comando do passo 5: `10 recorded, 0 pending` e `pending pairs 0`.
   11. Meça com `.venv\Scripts\python -m bench.prompts --summary docs/research/replies-summary.json` (alguns minutos, no PC).
   12. Diga ao Claude «gravação das respostas feita», para ele conferir os números e escrevê-los na documentação.
2. **A definição fica ligada por omissão.** Recomendação: mantê-la ligada. Sem mensagem emparelhada, o botão 5 é o mesmo de antes (medido acima) e, com mensagem, a correção só pode trocar palavras que soam parecido, nunca acrescentar. Se preferir esperar pelos números das respostas, desligue-a com `last_reply_context = false` em `[claude_code]` de `local\quill.toml` (veja o [guia de uso](../USAR.md)).
3. **O limite da alternativa sem nota.** Recomendação: aceitar o limite. Sem nota do aviso, a conversa mais recente da pasta pode ser uma execução automática no mesmo projeto. A alternativa é usar só a nota (sem a conversa mais recente), o que deixa sem contexto as conversas que ainda não terminaram nenhuma resposta. Diga se a quer.
4. **A meta de 12 % nos prompts continua por cumprir (21,71 %).** Continua a recomendação da Fase 11: gravar mais prompts para o Claude Code. Custo 0 €.
5. **Continuam abertas as decisões das fases anteriores**, incluindo a do motor e do custo em [ENGINES.md](ENGINES.md) e as das Fases 7 a 11 ([FASE7.md](FASE7.md), [FASE8.md](FASE8.md), [FASE9.md](FASE9.md), [FASE11.md](FASE11.md)).
