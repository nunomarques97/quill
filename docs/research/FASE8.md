# Quill: relatório da Fase 8 (termos do domínio no botão 5 para o Claude Code)

**Estado: concluída a 2026-10-03, sem o Sponsor presente.** Este documento tem só números agregados: nenhum texto falado, nenhum nome de projeto, nenhum termo real e nenhum caminho da máquina. O texto de cada gravação fica em `bench/results/` (ignorado pelo Git). Tudo correu no PC (Whisper e o Ollama partilhado), sem instalar nada, sem tirar nem apagar modelos de outros projetos e com custo 0 €.

## Resultado

A meta dos termos do domínio foi **cumprida**: no texto final do botão 5, nos 15 prompts gravados, ficaram **3 erros em 16 termos** (meta: no máximo metade dos 7 de hoje, ou seja 3). Na Fase 7 eram 4. Continuam 0 palavras perdidas, 0 inventadas e 0 palavras do projeto fora do «Contexto». A meta não foi mexida.

## O que mudou (T1 a T4)

| Tarefa | O que mudou |
|---|---|
| T1: dicas pelo que já foi ouvido | Com o botão 5 no Claude Code e o projeto reconhecido, o Whisper continua a receber o nome do projeto e termos do pacote como dicas, mas agora os termos do pacote que **soam parecido com o que já ouviu** neste ditado passam para a frente da parte do projeto. A comparação usa as mesmas chaves de som da correção (k/c/q, ph/f, y/i, h mudo, letras dobradas e w/u/v iguais), com poucas letras de diferença: os termos com menos de 4 letras só contam se forem iguais, e um termo com várias partes pode corresponder a 2 ou 3 palavras ouvidas. O espaço é o mesmo de antes (110 caracteres para o projeto, 330 no total). Sem nada ouvido, as dicas são exatamente as da Fase 7. O registo só guarda o motivo e o número de trocas de dicas, nunca termos nem texto. |
| T2: medição | `bench.prompts` passa cada gravação três vezes pela transcrição: com as dicas de hoje, com as dicas da Fase 7 (só pela relevância) e com as dicas pelo que foi ouvido (o caminho da aplicação). |
| T3: lista de termos na correção | A correção pode receber primeiro uma lista dos termos do pacote que soam parecido com o ditado. A verificação, a regra «só termos» e a tolerância das chaves de som não mudaram. Medida com e sem a lista (em baixo): não ajudou, por isso **ficou desligada**. |
| T4: medição e ajuste | As dicas mudavam para trás e para a frente enquanto as palavras provisórias da transcrição mudavam (até 25 trocas num ditado curto), e cada troca obriga a transcrever outra vez o fim do ditado. Agora, um termo ouvido uma vez fica nas dicas até ao fim desse ditado; as dicas só mudam quando se ouve um termo novo ou um termo fica mais parecido. Trocas nos prompts: 59 para 43; nos ditados: 35 para 10. |

## Números medidos com a sua voz

Medidos com `.venv\Scripts\python -m bench.prompts` nas gravações que já existiam: os 15 prompts gravados para 4 projetos e os 9 ditados para o Claude Code. O resumo só com números está em [prompts-summary.json](prompts-summary.json). Nenhuma gravação nova.

| Medição | Hoje (correção de antes) | Dicas de hoje | Fase 7 (dicas por relevância) | Fase 8 (dicas pelo que foi ouvido) | Meta | Cumprida |
|---|---|---|---|---|---|---|
| Erros em termos do domínio depois da transcrição e do pipeline (prompts) | 7 de 16 | 7 de 16 | 5 de 16 | 4 de 16 | — | — |
| Erros em termos do domínio no texto final (prompts) | 7 de 16 | 5 de 16 | 4 de 16 | **3 de 16** | no máximo 3 | **sim** |
| Palavras de conteúdo perdidas (24 gravações) | 1 | 1 | 0 | 0 | 0 | sim |
| Palavras de conteúdo inventadas (24 gravações) | 5 | 0 | 0 | 0 | 0 | sim |
| Palavras do projeto fora do «Contexto» | — | 0 | 0 | 0 | 0 | sim |
| Enriquecimento: pedidos / aceites (24 gravações) | — | 22 / 20 | 20 / 19 | 18 / 17 | — | — |

As colunas «Dicas de hoje», «Fase 7» e «Fase 8» são o botão 5 novo com as três dicas do Whisper, na mesma medição. «Hoje» é a correção do botão 5 antes da Fase 6.

Latência, em segundos (p50 / p95), na mesma medição:

| Etapa | Prompts, Fase 7 | Prompts, Fase 8 | Ditados, Fase 7 | Ditados, Fase 8 |
|---|---|---|---|---|
| Transcrição (do fim da fala ao texto) | 0,29 / 0,78 | 0,32 / 0,59 | 0,38 / 1,28 | 0,38 / 1,28 |
| Correção | 1,09 / 2,19 | 1,02 / 2,14 | 1,08 / 1,61 | 1,08 / 1,61 |
| Enriquecimento | 1,76 / 3,19 | 1,82 / 4,17 | 1,21 / 2,30 | 1,27 / 2,72 |
| Botão 5 depois da transcrição | 2,69 / 5,86 | 2,67 / 6,43 | 2,18 / 3,62 | 2,26 / 4,18 |

Nenhuma chamada ao modelo passou os limites da aplicação (4 s na correção, 15 s no enriquecimento). Durante as medições, o PC tinha outros programas a usar a placa gráfica e o processador (por exemplo, a leitura do pacote de um projeto chegou a 3,4 s), por isso as latências estão acima das da Fase 7 (botão 5 nos prompts, 1,72 / 2,24 s) em todas as colunas. A comparação justa é entre colunas da mesma medição.

**Transcrição mais lenta por trocar de dicas?** A regra desta fase era: no máximo +0,15 s no p95 da transcrição dos prompts, contra as dicas da Fase 7 na mesma medição. Antes do ajuste do T4, deu +0,05, +0,47 e +0,16 s em três medições (com 15 prompts, o p95 é o mais lento de todos, e um só atraso da placa gráfica muda-o; num ditado sem nenhuma troca houve +0,32 s). Depois do ajuste: +0,06 s e −0,20 s em duas medições, com as mesmas contagens nas duas.

## Lista de termos na correção (T3): medida e desligada

Duas medições seguidas, iguais exceto a lista (`--no-correction-candidates`), ambas antes do ajuste do T4:

| Medição | Com a lista | Sem a lista |
|---|---|---|
| Erros em termos do domínio no texto final, dicas da Fase 8 | 2 de 16 | 2 de 16 |
| Erros em termos do domínio no texto final, dicas da Fase 7 | 4 de 16 | 4 de 16 |
| Palavras perdidas / inventadas (dicas da Fase 8) | 0 / 0 | 0 / 0 |
| Enriquecimento pedidos / aceites nos prompts (dicas da Fase 8) | 12 / 11 | 12 / 11 |
| Enriquecimento pedidos / aceites nos prompts (dicas da Fase 7) | 11 / 10 | 13 / 12 |
| Enriquecimento pedidos / aceites nos prompts (dicas de hoje) | 12 / 10 | 14 / 12 |

A lista não corrigiu nenhum termo a mais e, com ela, a verificação recusou mais correções; uma correção recusada não é enriquecida. Por isso a lista ficou desligada na aplicação (`quill.autorewrite.LIKELY_TERMS`). Para a voltar a medir: `.venv\Scripts\python -m bench.prompts --correction-candidates`.

## Os 3 erros que ficaram (lidos nos ficheiros ignorados)

1. Um termo ouvido como uma palavra comum que soa quase igual; a correção deixou-a como estava.
2. Um termo dito ao lado de um número, ouvido como outro número; a verificação protege números de propósito, por isso a correção nunca o pode mudar.
3. Um termo que o próprio projeto escreve de duas formas (com e sem hífen): o Whisper escreveu a outra forma. Com as dicas antes do ajuste do T4 tinha ficado certo; é por isto que o ajuste passou de 2 para 3 erros.

## Decisões para o Sponsor

1. **Menos prompts enriquecidos depois de uma correção recusada (18 pedidos contra 20 com as dicas da Fase 7).** Com as dicas novas, 3 prompts foram ouvidos de outra forma e a verificação (que não mudou) recusou a correção deles; em 2, o termo já vinha certo do Whisper e o modelo da correção tentou mudar outras palavras. Noutro prompt, a correção passou a ser aceite. Saldo: 2 pedidos a menos. Hoje, quando a correção é recusada, o Quill escreve o que ouviu e não enriquece o prompt; o texto vai certo, mas sem «Pedido» e «Contexto». Dos pedidos de enriquecimento, foram aceites 17 de 18; o único recusado também é recusado com as dicas da Fase 7 (o pacote desse projeto mudou desde a Fase 7, porque os ficheiros do projeto mudaram). Recomendação: quando a correção é recusada, enriquecer o texto ouvido, com a mesma verificação do enriquecimento. Custo 0 €, nada a instalar; mede-se nas mesmas gravações.
2. **Meta cumprida à justa (3 de 3).** Se quiser margem, o próximo passo gratuito é o erro 3: acrescentar ao seu vocabulário a forma que usa para um termo que o projeto escreve de duas maneiras (veja «Acrescentar palavras ao vocabulário» em [../USAR.md](../USAR.md)). Os nomes do vocabulário vão sempre à frente nas dicas do Whisper.
3. **Continuam abertas as decisões da Fase 7** (ler os prompts enriquecidos e a acessibilidade do VS Code; veja [FASE7.md](FASE7.md)). Os prompts enriquecidos desta medição estão em `bench/results/prompts/20261003-131743/exemplos.md`.

## Como verificar

1. Ligue o Quill como de costume (`.venv\Scripts\python -m quill`).
2. No VS Code, clique na caixa de texto de uma conversa do Claude Code de um projeto com pacote de contexto.
3. Mantenha premido o botão 5, diga um pedido com um termo do projeto que o Whisper costumava escrever mal e largue. O termo deve aparecer bem escrito, o Quill deve carregar uma vez em Enter e o indicador deve mostrar «Prompt enriquecido e enviado».
