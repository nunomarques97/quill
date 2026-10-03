# Quill: relatório da Fase 9 (nomes mal ouvidos e correções de bom senso no botão 5 para o Claude Code)

**Estado: concluída a 2026-10-03, sem o Sponsor presente.** Este documento tem só números agregados: nenhum texto falado, nenhum nome de projeto, nenhum termo real e nenhum caminho da máquina. O texto de cada gravação fica em `bench/results/prompts/20261003-190047/` (ignorado pelo Git). Tudo correu no PC (Whisper e o Ollama partilhado, com o qwen3:8b), sem instalar nada, sem tirar nem apagar modelos de outros projetos e com custo 0 €.

## Resultado

- **Nomes do vocabulário:** com o botão 5 no Claude Code, uma palavra com maiúscula que soa como um nome do seu vocabulário (ou como o nome do projeto, um termo do vocabulário ou um termo do pacote) já pode ser trocada por esse nome. Um passo fixo, antes do modelo, escreve o nome quando o som é exatamente o mesmo, mesmo que o modelo não o faça. O caso do registo de hoje (um nome com o som de «j», escrito pelo Whisper com «z» e maiúscula) passa a sair certo neste passo fixo; foi verificado com o seu vocabulário, sem modelo e sem som. **Nenhuma gravação que já existe tem um nome do vocabulário mal ouvido**, por isso a correção de nomes ainda não foi medida com a sua voz: veja «Gravações que faltam».
- **Termos do domínio nos 15 prompts gravados:** 2 erros em 16 (Fase 8: 3). A meta (no máximo 3) continua cumprida, agora com margem.
- **Correções recusadas por causa de um nome:** 0 (Fase 8: 1). Essa correção passou a ser aceite e o prompt passou a ser enriquecido.
- **Correções de bom senso:** medidas, mas **ficam desligadas**. Baixaram só 1 erro de palavra em 193 e, em 8 correções, 5 trouxeram palavras que não disse (e uma tirou uma palavra que disse). A regra desta fase só as deixava ligar com 0 perdidas e 0 inventadas em todos os conjuntos. Fica para decisão sua (em baixo).
- Continuam 0 palavras perdidas, 0 inventadas e 0 palavras do projeto fora do «Contexto» no botão 5 (24 gravações). Nenhuma meta foi mexida.

## O que estava errado

1. **O nome mal ouvido era sempre escrito como o Whisper o ouviu.** O Whisper escreve o nome mal ouvido com maiúscula, e a verificação protege qualquer palavra com maiúscula: recusava a correção inteira com o motivo `name` antes de olhar se a troca era por um nome do vocabulário. Além disso, a comparação dos sons não juntava as confusões do português europeu que o Whisper faz (j, z e g antes de e/i; c antes de e/i e ç com s), por isso o nome certo e o mal ouvido não «soavam parecido».
2. **Enriquecimento recusado com `number` em ditados sem números.** O pedido ao modelo trazia uma frase do resumo do projeto para copiar palavra a palavra, e essa frase podia ter um número. O modelo copiava-a para o «Contexto» e a verificação recusa sempre um número que não foi ditado. Agora o resumo e os termos do projeto que o enriquecimento vê não têm palavras com algarismos. A regra dos números ficou igual: um número acrescentado, perdido ou mudado é sempre recusado.
3. **No Claude Code, só termos podiam substituir palavras.** A regra «só termos» (Fase 6) desfaz qualquer troca por palavras comuns. Frases como «tudo cedo» em vez de «tu decides» ou «museu local» em vez de «modelo local» ficavam como o Whisper as ouviu.
4. **Encontrado nesta medição:** a verificação também recusava a correção inteira quando o modelo escrevia uma palavra com maiúscula na forma da lista, com as mesmas letras (por exemplo, um termo do pacote ouvido sem o hífen que a lista tem, ou o nome do projeto com outras maiúsculas e acentos). Era a única recusa por `name` nos prompts.

## O que mudou (T1 a T5)

| Tarefa | O que mudou |
|---|---|
| T1: nomes | Só com o botão 5 no Claude Code. As chaves de som passam a juntar, para o português europeu, j, z e g antes de e/i num só som e c antes de e/i e ç com s. Um passo fixo, antes do modelo, escreve um grupo de 1 a 3 palavras como um nome do seu vocabulário ou o nome do projeto quando as chaves de som (com pelo menos 4 letras) são iguais; nunca números, palavras pequenas da língua nem termos que já estão na lista. As instruções do modelo dizem que uma palavra com maiúscula pode ser um nome mal ouvido, e a verificação deixa trocar uma palavra com maiúscula só por um nome ou termo da lista que soe parecido. Uma palavra comum nunca substitui um nome, e um número nunca muda. Os nomes do vocabulário já iam primeiro nas dicas do Whisper e nas instruções da correção; ficou provado em testes. |
| T2: `number` no enriquecimento | A causa e a correção estão no ponto 2 acima. Nas gravações não houve nenhuma recusa por `number`, nem antes nem depois; a causa foi reproduzida em testes com dados inventados. |
| T3: bom senso | Nova opção `common_sense_fixes` em `[autorewrite]`, desligada por omissão. Ligada, o modelo pode trocar um grupo curto (no máximo 3 palavras) que não faz sentido na frase por palavras comuns que soam parecido. A verificação só aceita a troca se o som for parecido em português europeu (com as palavras juntas ou separadas de outra forma), se não mexer em nomes, números, termos da lista, negações ou condições, e se não trouxer mais nenhuma palavra. |
| T4: medição | `bench.prompts --variants --safety` mede, na mesma medição e sobre as mesmas transcrições, a correção da Fase 8, a correção com nomes (a da aplicação) e a correção com nomes e bom senso. Conta os erros de palavras contra o texto certo e as palavras inventadas contra o ditado e contra o texto certo. |
| T5: medição e correção | Esta medição. Encontrou o ponto 4 de «O que estava errado»: a verificação passou a aceitar uma palavra com maiúscula escrita na forma da lista com as mesmas letras. A troca por outro nome, ou com uma palavra a mais, continua recusada. Correções de bom senso: ficam desligadas (em baixo). |

## Números medidos com a sua voz

Comando: `.venv\Scripts\python -m bench.prompts --variants --safety`, nas gravações que já existiam: os 15 prompts gravados para 4 projetos, os 9 ditados para o Claude Code e, como conjunto de segurança, os 36 ditados válidos como se fossem ditos para o Claude Code sem projeto (sem enriquecimento). As três colunas correm sobre as mesmas transcrições, na mesma medição. O resumo só com números está em [prompts-summary.json](prompts-summary.json). Nenhuma gravação nova.

| Medição | Fase 8 | Fase 9 (nomes; a aplicação) | Fase 9 com bom senso | Meta | Cumprida |
|---|---|---|---|---|---|
| Erros em termos do domínio no texto final (prompts; 7 com a correção de antes) | 3 de 16 | **2 de 16** | 2 de 16 | no máximo 3 | sim |
| Nomes em falta depois da correção (prompts / ditados / segurança) | 5 de 15 / 0 de 4 / 0 de 9 | 5 de 15 / 0 de 4 / 0 de 9 | 5 de 15 / 0 de 4 / 0 de 9 | — | — |
| Erros de palavras contra o texto certo, prompts (71 de 304 depois da transcrição) | 70 (23,0 %) | **68 (22,4 %)** | 68 (22,4 %) | — | — |
| Erros de palavras, ditados (25 de 253 depois da transcrição) | 25 (9,9 %) | 25 (9,9 %) | 24 (9,5 %) | — | — |
| Erros de palavras, segurança (102 de 960 depois da transcrição) | 102 (10,6 %) | 100 (10,4 %) | 100 (10,4 %) | — | — |
| Palavras perdidas (prompts / ditados / segurança) | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / **1** | 0 | só sem bom senso |
| Palavras inventadas contra o ditado, fora das correções de bom senso | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 | sim |
| Palavras trazidas pelas correções de bom senso | — | — | 3 / 1 / 4 | — | — |
| Palavras inventadas contra o texto certo (prompts / ditados / segurança) | 1 / 0 / 0 | 1 / 0 / 0 | **4 / 0 / 3** | 0 | só sem bom senso |
| Correções recusadas com `name` (prompts) | 1 | 0 | 0 | — | — |
| Enriquecimento: pedidos / aceites (prompts + ditados) | 17 / 14 | **18 / 16** | 18 / 16 | — | — |

A palavra inventada contra o texto certo nas colunas «Fase 8» e «Fase 9» é a mesma: a correção pôs o acento numa palavra que o Whisper ouviu mal (a palavra certa era outra). Já acontecia na Fase 8 e não é uma palavra nova para a contagem contra o ditado.

Latência, em segundos (p50 / p95), na mesma medição:

| Etapa | Fase 8 | Fase 9 (nomes) | Fase 9 com bom senso |
|---|---|---|---|
| Transcrição, prompts / ditados (igual nas três) | 0,26 / 0,35 e 0,23 / 0,33 | igual | igual |
| Correção, prompts | 0,64 / 0,85 | 0,63 / 0,91 | 0,68 / 2,87 |
| Correção, ditados | 1,05 / 1,40 | 0,94 / 1,41 | 1,00 / 1,60 |
| Correção, segurança | 0,69 / 1,38 | 0,65 / 1,16 | 0,69 / 1,11 |
| Botão 5 depois da transcrição, prompts | 1,93 / 3,13 | 2,13 / 2,83 | 2,09 / 4,87 |
| Botão 5 depois da transcrição, ditados | 1,67 / 3,21 | 1,73 / 3,05 | 1,82 / 3,60 |

Nenhuma correção nova nem nenhum enriquecimento passou os limites da aplicação (4 s na correção, 15 s no enriquecimento). A única chamada acima do limite foi a primeira da medição (a correção de antes, 8,6 s, com o modelo ainda a carregar).

Uma primeira medição, logo antes da correção do ponto 4, deu os mesmos números da Fase 8 e da Fase 9 com bom senso nos ditados e na segurança, e na Fase 9 nos prompts 3 erros em termos, 70 erros de palavras e 1 recusa por `name`. Foi essa medição que mostrou o problema.

## Porque os nomes em falta não mudaram (5 de 15 nos prompts)

Lidos um a um nos ficheiros ignorados:

1. Em 3, o Whisper escreveu o nome do projeto como o próprio projeto o escreve (com maiúsculas e um acento), e o guião usa o nome da pasta, sem acento. A medição conta o acento como erro. Não é um nome mal ouvido.
2. Em 1, o mesmo nome saiu em duas palavras. O passo fixo não mexe num nome que já está escrito com as mesmas letras.
3. Em 1, o nome saiu como outra palavra que não soa parecido. A verificação não deixa trocar o que não soa parecido, de propósito.

Nos 36 ditados do conjunto de segurança (que incluem os 9 para o Claude Code), os 9 nomes ditos saíram todos certos. Nas transcrições guardadas das 44 gravações antigas do outro guião, que dizem nomes do vocabulário 38 vezes, faltam 2, e nenhum dos 2 soa parecido com o que o Whisper escreveu. Por isso nenhuma gravação que já existe tem o erro de hoje.

## Correções de bom senso: ficam desligadas

Foram 8 correções nos três conjuntos (3 nos prompts, 1 nos ditados, 4 na segurança). Lidas uma a uma:

- **3 certas:** formas de verbo ou palavras ouvidas com um som trocado (por exemplo, o tipo de erro de «museu local» por «modelo local»).
- **5 erradas:** trouxeram uma palavra que não disse, que soa parecido mas não é a certa; numa delas, mudou a forma de um verbo que estava certo (é a palavra perdida).

No total, baixaram 1 erro de palavra em 193 (192 contra 193), e as palavras que não disse subiram de 1 para 7: 5 trazidas pelas correções de bom senso e 1 por uma troca por um termo que o modelo só fez com estas instruções. A regra desta fase pedia menos erros com 0 perdidas e 0 inventadas em todos os conjuntos: **não foi cumprida**, por isso a opção fica desligada (`common_sense_fixes = false`). A regra «só termos» continua a ser a da aplicação.

## Como verificar

1. Ligue o Quill como de costume (`.venv\Scripts\python -m quill`). Em `local\logs\quill.log`, a linha `Quill started` diz `(common-sense fixes off)`.
2. No VS Code, clique na caixa de texto de uma conversa do Claude Code, mantenha premido o botão 5, diga um pedido com o nome do seu vocabulário que hoje saiu mal (por exemplo «no <nome>, corre os testes») e largue.
3. O nome deve aparecer escrito como está no vocabulário. Em `local\logs\quill.log`, a linha `autorewrite:` desse ditado não deve dizer `autorewrite_refused (name)`; quando foi o passo fixo a corrigir, diz `1 names fixed`.

## Gravações que faltam

Para medir os nomes com a sua voz são precisas gravações novas com um nome do vocabulário que o Whisper ouve mal. Ainda não há frases para isso no guião.

1. Diga ao Claude Code: «acrescenta ao guião de prompts 6 frases com um `<projeto-5>`, cada uma com o nome em sítios diferentes da frase».
2. Abra `local\bench.toml` e, em `[prompts.projects]`, acrescente a linha `"<projeto-5>" = "<nome>"`, com o nome do vocabulário que saiu mal hoje, escrito como está no vocabulário.
3. No Terminal do Windows, na pasta do Quill, corra `py -3.12 -m bench.record --set prompts`. Leia em voz alta cada frase nova que aparece no ecrã (Enter para começar e Enter para acabar cada uma), com a sua voz normal.
4. Corra `.venv\Scripts\python -m bench.prompts --variants --safety` e peça ao Claude Code para pôr os números deste relatório ao lado dos novos.

## Decisões para o Sponsor

1. **Correções de bom senso desligadas.** Recomendação: mantê-las desligadas. Nesta medição, 5 das 8 trocaram uma palavra certa ou mal ouvida por outra que também não disse, e só baixaram 1 erro em 193. Se as quiser experimentar no dia a dia, escreva `common_sense_fixes = true` em `[autorewrite]` de `local\quill.toml`; são sempre só grupos curtos, nunca nomes, números ou negações. Custo 0 €.
2. **Nomes do vocabulário ainda sem medição com a sua voz.** Recomendação: gravar as 6 frases de «Gravações que faltam» (cerca de 5 minutos). Custo 0 €, nada a instalar.
3. **Nome de projeto com duas grafias.** O projeto escreve o próprio nome com maiúsculas e acento e a pasta não. Recomendação: se quiser uma das formas sempre, acrescente-a ao vocabulário com `py -3.12 -m quill.vocabulary --add-name "<forma que quer>"`. Até lá, a medição conta a outra forma como erro.
4. **Os 2 erros de termos que ficaram:** um termo ouvido como uma palavra comum que soa quase igual (o modelo deixou-a como estava) e um termo dito ao lado de um número, ouvido como outro número (a verificação protege números de propósito). A meta está cumprida; não há nada a decidir agora.
5. **Continuam abertas as decisões das Fases 7 e 8** (veja [FASE7.md](FASE7.md) e [FASE8.md](FASE8.md)). Os prompts enriquecidos desta medição estão em `bench/results/prompts/20261003-190047/exemplos.md`.
