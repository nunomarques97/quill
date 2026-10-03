# Quill: relatório da Fase 11 (o que o Whisper ouve mal no botão 5 para o Claude Code)

**Estado: concluída a 2026-10-03, sem o Sponsor presente.** Este documento tem só números agregados: nenhum texto falado, nenhum nome de projeto, nenhum termo real e nenhum caminho da máquina. O texto de cada gravação fica em `bench/results/asr/20261003-213025/` (diagnóstico) e `bench/results/prompts/20261003-231103/` (medição do botão 5), as duas pastas ignoradas pelo Git. Tudo correu no PC (os dois modelos Whisper que já estavam no disco e o Ollama partilhado, com o qwen3:8b), sem instalar nem descarregar nada, sem tirar nem apagar modelos de outros projetos e com custo 0 €.

## Resultado

- **A meta de 12 % nos prompts não foi cumprida.** Depois do botão 5 completo, os 15 prompts gravados continuam com **68 erros de palavras em 304 (22,37 %)**, como na Fase 9. Para chegar aos 12 % teriam de ser no máximo 36 erros.
- **De onde vêm os erros:** não vêm do streaming, do corte do silêncio, da língua detetada, do beam, do fallback de temperatura nem do prompt inicial. Mudar cada uma destas coisas tira no máximo 1 ou 2 erros em cerca de 70 (em baixo). O que mais pesa são as dicas do Whisper, e no bom sentido: sem elas há 93 erros em vez de 70. Os erros que ficam são sobretudo palavras comuns, não termos (65 dos 71), que o modelo ouve mal com qualquer configuração, incluindo o large-v3.
- **Novo no botão 5: uma segunda passagem do Whisper sobre o áudio todo, ao largar** (`[final_pass]`). Foi feita, testada e medida com 5 configurações. Nenhuma baixou os erros dos prompts mantendo os termos do domínio em no máximo 3 de 16, por isso **fica desligada** (`enabled = false`). O botão 5 escreve o mesmo texto que antes e demora o mesmo.
- Continuam 0 palavras perdidas, 0 inventadas e 0 palavras do projeto fora do «Contexto» no botão 5 (24 gravações), e os termos do domínio ficam em 2 de 16 (meta: no máximo 3). Nenhuma meta foi mexida.
- **Nenhum modelo novo ajudaria pelo que foi medido:** o large-v3 e o large-v3-turbo, os dois já no disco, diferem em 1 ou 2 erros em 304. Por isso não fica nenhum comando de descarga para aprovar.

## De onde vêm os erros (T1)

Comando: `.venv\Scripts\python -m bench.asr_errors`. Cada gravação foi transcrita de novo, no PC, com 20 configurações; cada configuração muda uma só coisa em relação àquela com que é comparada. Os erros são contados contra o texto do guião, depois da transcrição (sem correção nem enriquecimento), com a mesma normalização da medição do botão 5. O resumo só com números está em [asr-errors-summary.json](asr-errors-summary.json).

Conjuntos: os 15 prompts gravados (304 palavras, 95,2 s de áudio, 6,108 s a meio da lista), os 9 ditados para o Claude Code (253 palavras), os 36 ditados válidos (960 palavras) e as 20 instruções de reescrita (106 palavras).

**Como são os erros dos prompts hoje** (a transcrição em streaming da aplicação, 71 erros em 304, 23,36 %; as categorias sobrepõem-se):

| Tipo | Erros |
|---|---|
| Palavras trocadas / em falta / a mais | 48 / 16 / 7 |
| Uma palavra ouvida como duas ou mais (o tipo de «falha» ouvido como «file é») | 3 casos, 6 erros |
| Duas ou mais palavras ouvidas como uma | 9 casos, 18 erros |
| Nas duas primeiras e duas últimas palavras de cada prompt (60 palavras) | 21 erros |
| Nas palavras dos termos do domínio (18 palavras) | 6 erros |
| Nas outras palavras (286 palavras) | 65 erros |
| Termos do domínio errados (16 ocorrências) | 4 |
| Língua detetada | português nas 15 gravações |

**Cada causa isolada** (erros nos prompts depois da transcrição, contra a configuração com que é comparada):

| Causa | Configuração | Erros | Comparada com | Termos do domínio |
|---|---|---|---|---|
| Streaming e colagem dos pedaços | o áudio todo de uma vez | 70 (23,03 %) | streaming, 71 (23,36 %) | 3 (antes 4) |
| Corte do silêncio | sem corte | 75 (24,67 %) | áudio todo, 70 | 5 |
| Corte do silêncio | filtro de voz (VAD) do Whisper | 69 (22,70 %) | áudio todo, 70 | 3 |
| Modelo | large-v3, áudio todo | 69 (22,70 %) | turbo, 70 | 4 |
| Modelo | large-v3, beam 10 | 68 (22,37 %) | turbo, beam 10, 70 | 4 |
| Modelo | large-v3 em streaming | 80 (26,32 %) | turbo em streaming, 71 | 6 |
| Dicas | sem dicas | 93 (30,59 %) | com as dicas da aplicação, 70 | 6 |
| Dicas | só o vocabulário (sem os termos do projeto) | 89 (29,28 %) | 70 | 7 |
| Espaço das dicas | 150 caracteres em vez de 330 | 69 (22,70 %) | 70 | 3 |
| Espaço das dicas | 600 caracteres | 183 (60,20 %) | 70 | 6 |
| Língua | detetada pelo modelo em vez de português | 70 (23,03 %) | 70 | 3 |
| Beam | 1 / 10 em vez de 5 | 79 (25,99 %) / 70 (23,03 %) | 70 | 5 / 3 |
| Fallback de temperatura | ligado | 70 (23,03 %) | 70 | 3 |
| Texto anterior como contexto | ligado | 70 (23,03 %) | 70 | 3 |
| Prompt inicial | só hotwords / só prompt / com uma frase de instrução | 72 / 73 / 70 | 70 | 5 / 3 / 3 |

O que isto quer dizer:

1. **Streaming:** ouvir o áudio todo de uma vez tira só 1 erro. A colagem dos pedaços não é a causa.
2. **Corte do silêncio:** sem corte há 5 erros a mais; o filtro de voz do Whisper tira 1 nos prompts, mas põe 5 a mais nos 9 ditados e 4 a mais nas reescritas. Os 21 erros no início e no fim dos prompts ficam com qualquer corte (20 sem corte, 19 com o filtro).
3. **Modelo:** o large-v3 tira 1 a 2 erros nos prompts, mas erra mais 1 termo do domínio e, nos 36 ditados, tem 134 erros em vez de 125.
4. **Dicas:** são o que mais ajuda (sem elas, 93 erros e 6 termos errados). Não estão a «empurrar» palavras erradas: cortar o espaço para 150 caracteres tira só 1 erro nos prompts e põe 6 nas reescritas. Com 600 caracteres o Whisper começa a escrever a lista de dicas em vez do que disse (183 erros).
5. **Língua, beam maior, fallback de temperatura, texto anterior e frase de instrução:** 0 erros a menos nos prompts. A língua é sempre detetada como português nos prompts; nas reescritas, a deteção automática saiu noutra língua em 3 de 20.

Nos outros conjuntos, com a transcrição da aplicação: 9 ditados para o Claude Code 31 erros em 253 (12,25 %), 36 ditados 125 em 960 (13,02 %), reescritas 22 em 106 (20,75 %). Os prompts têm o dobro dos erros dos ditados com o mesmo modelo e as mesmas definições, e a diferença não muda com nenhuma das configurações acima.

Tempo de cada transcrição dos prompts (p50 / p95): streaming turbo 0,229 / 0,278 s; áudio todo turbo 0,259 / 0,293 s; áudio todo large-v3 0,584 / 0,763 s; large-v3 com beam 10 0,703 / 0,851 s.

## O que mudou (T1 a T5)

| Tarefa | O que mudou |
|---|---|
| T1: diagnóstico | Novo `bench.asr_errors`, que transcreve de novo as gravações com as 20 configurações acima e classifica os erros (em baixo, «Como verificar»). O Whisper ganhou quatro opções (língua detetada, fallback de temperatura, texto anterior como contexto e filtro de voz), todas desligadas por omissão: o botão 4, o botão do meio, o botão 5, o modo comando e os comandos de voz transcrevem exatamente como antes. |
| T2: passagem final | O Quill sabe transcrever outra vez, de uma só vez, o áudio de um ditado já largado, com as dicas desse ditado. Corre no mesmo fio do modelo, por isso nunca há duas transcrições ao mesmo tempo no mesmo modelo. Se falhar, demorar mais do que `timeout_s` ou não der texto, fica o texto do streaming. |
| T3: ligação ao botão 5 | Nova secção `[final_pass]` em `quill.toml`. Só o botão 5 para o Claude Code a usa; o botão 5 noutras janelas, o botão 4, o botão do meio, o modo comando e os comandos de voz ficam como estavam. Com o large-v3, a passagem usa a mesma cópia do modelo que os comandos de voz (não carrega outra). O registo diz se a passagem está ligada e, em cada ditado, o motivo e os milissegundos, nunca o texto. |
| T4: medição | `bench.prompts --pass-candidates all` mede o botão 5 completo com o texto do streaming e com 5 passagens finais, nos prompts, nos 9 ditados e nos 36 ditados de segurança. Nenhuma passagem cumpriu as regras para ficar ligada, por isso `quill.example.toml` traz `[final_pass] enabled = false`. |
| T5: relatório | Este documento, o [README](../../README.md) e o [guia de uso](../USAR.md). |

## Números medidos com a sua voz (antes e depois)

Comando: `.venv\Scripts\python -m bench.prompts --variants --safety --pass-candidates all`, com o qwen3:8b no Ollama partilhado, nas gravações que já existiam: os 15 prompts, os 9 ditados para o Claude Code e, como conjunto de segurança, os 36 ditados válidos como se fossem ditos para o Claude Code sem projeto (sem enriquecimento). Todas as colunas correm na mesma medição. O resumo só com números está em [prompts-summary.json](prompts-summary.json), na parte `final_pass`. «Antes» é o botão 5 de hoje (o texto do streaming), que é o que fica.

Passagens medidas: large-v3 com beam 10, large-v3 com beam 5, large-v3 com beam 10 e fallback de temperatura, large-v3 com beam 10 e filtro de voz, e turbo com beam 10. Todas com as dicas do ditado, o corte do silêncio da aplicação e 3 s de limite; as 15 + 9 + 36 passagens deram texto em todas.

**Erros de palavras contra o texto certo** (depois da transcrição / depois da limpeza / botão 5 completo):

| Conjunto | Antes (streaming) | large-v3 beam 10 | large-v3 beam 5 | large-v3 beam 10 + fallback | large-v3 beam 10 + filtro de voz | turbo beam 10 |
|---|---|---|---|---|---|---|
| Prompts (304 palavras) | 71 / 71 / **68 (22,37 %)** | 68 / 69 / 69 (22,70 %) | 69 / 70 / 70 (23,03 %) | 68 / 69 / 69 (22,70 %) | 66 / 67 / 67 (22,04 %) | 70 / 70 / 69 (22,70 %) |
| Ditados para o Claude Code (253) | 31 / 25 / **25 (9,88 %)** | 31 / 26 / 26 (10,28 %) | 31 / 26 / 26 (10,28 %) | 31 / 26 / 26 (10,28 %) | 27 / 22 / 22 (8,70 %) | 31 / 25 / 25 (9,88 %) |
| Segurança (960) | 127 / 102 / **100 (10,42 %)** | 135 / 115 / 115 (11,98 %) | 134 / 117 / 117 (12,19 %) | 135 / 115 / 115 (11,98 %) | 125 / 107 / 107 (11,15 %) | 128 / 103 / 102 (10,62 %) |

**As outras metas:**

| Medição | Antes | large-v3 beam 10 | large-v3 beam 5 | + fallback | + filtro de voz | turbo beam 10 | Meta |
|---|---|---|---|---|---|---|---|
| Termos do domínio errados, prompts (depois da limpeza / botão 5 completo) | 4 / **2 de 16** | 4 / 4 | 4 / 4 | 4 / 4 | 4 / 4 | 3 / 2 | no máximo 3 |
| Nomes em falta, prompts (de 15) | 5 | 4 | 4 | 4 | 5 | 5 | — |
| Nomes em falta, ditados (de 4) / segurança (de 9) | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | — |
| Palavras perdidas / inventadas (prompts, ditados e segurança) | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Enriquecimento pedido / aceite, prompts | 11 / 9 | 12 / 10 | 12 / 10 | 12 / 10 | 13 / 11 | 11 / 9 | — |
| Enriquecimento pedido / aceite, ditados | 7 / 6 | 9 / 7 | 9 / 7 | 9 / 7 | 9 / 7 | 8 / 7 | — |
| Do largar ao texto, p95, prompts / ditados / segurança (s) | 2,916 / 2,784 / 1,412 | 5,587 / 5,501 / 2,924 | 3,316 / 4,31 / 3,231 | 4,351 / 5,466 / 3,563 | 4,271 / 5,255 / 2,982 | 3,265 / 3,167 / 2,156 | no máximo 6 |
| Pode ficar ligada | — | não (termos) | não (termos) | não (termos) | não (termos) | cumpre, mas não baixa os erros | — |

As quatro passagens com o large-v3 erram 4 termos do domínio em 16 e passam a meta. A do large-v3 com filtro de voz é a que menos erra nos prompts (67 contra 68), mas erra mais nos 36 ditados (107 contra 100). A do turbo com beam 10 cumpre todas as metas, mas fica com 69 erros contra 68: não melhora, por isso também não fica ligada.

**Tempo de cada etapa nos prompts, em segundos (p50 / p95):**

| Etapa | Antes | large-v3 beam 10 | large-v3 beam 5 | + fallback | + filtro de voz | turbo beam 10 |
|---|---|---|---|---|---|---|
| Texto do streaming | 0,24 / 0,294 | igual | igual | igual | igual | igual |
| Passagem final | — | 0,759 / 1,026 | 0,601 / 0,841 | 0,745 / 0,889 | 0,77 / 1,045 | 0,336 / 0,42 |
| Limpeza | 0,002 / 0,002 | 0,002 / 0,003 | 0,002 / 0,002 | 0,002 / 0,002 | 0,002 / 0,003 | 0,002 / 0,002 |
| Correção | 0,762 / 0,88 | 0,616 / 0,815 | 0,625 / 0,809 | 0,554 / 0,732 | 0,679 / 0,992 | 0,753 / 0,88 |
| Enriquecimento | 1,503 / 1,884 | 1,312 / 1,715 | 1,374 / 1,709 | 1,247 / 1,631 | 1,565 / 1,917 | 1,503 / 1,884 |
| Do largar ao texto (total) | 2,292 / 2,916 | 2,84 / 5,587 | 2,862 / 3,316 | 2,92 / 4,351 | 3,071 / 4,271 | 2,653 / 3,265 |

Passagem final nos ditados (p50 / p95): 0,931 / 1,305 s com o large-v3 beam 10 e 0,384 / 0,543 s com o turbo; na segurança, 0,861 / 1,697 s e 0,386 / 0,744 s. Todas as gravações tinham até 20 s de áudio (o prompt mais longo tem 8,348 s e o ditado mais longo 18,048 s).

## Memória da placa gráfica

Medida só por leitura (`nvidia-smi` e a lista de modelos do Ollama), sem descarregar nada:

| Momento | Usada | Livre | No Ollama |
|---|---|---|---|
| Os dois modelos Whisper carregados, antes da medição do botão 5 | 13 241 MiB | 2 810 MiB | qwen3:8b (5 320 MiB) |
| Depois da medição do botão 5 | 7 780 MiB | 8 271 MiB | nenhum (o Ollama tirou-o sozinho) |
| Diagnóstico: turbo carregado | 12 666 MiB | 3 385 MiB | qwen3:8b (5 320 MiB) |

Os dois modelos Whisper e o qwen3:8b cabem juntos, com 2 810 MiB livres. Como a passagem fica desligada, o Quill carrega o mesmo que antes. Ligada com o large-v3, usa a cópia que os comandos de voz já carregam; se os comandos de voz estiverem desligados, o Quill carrega essa cópia na mesma.

## Meta dos 12 %: não cumprida

O melhor botão 5 completo continua a ser o de hoje: 68 erros em 304 (22,37 %). A melhor passagem medida fica em 67 (22,04 %), mas erra 4 termos do domínio. Os 12 % pediam no máximo 36 erros, e nenhuma opção grátis medida tira mais de 5 depois da transcrição (71 para 66, com o large-v3 e o filtro de voz). Pelo diagnóstico, os erros que ficam são do modelo a ouvir a sua voz nestes prompts, não das definições.

## Como verificar

1. No Terminal do Windows, na pasta do Quill, corra `.venv\Scripts\python -m bench.prompts --check docs\research\prompts-summary.json`. Deve dizer `met` em todas as linhas, menos na dos 12 %, que diz `NOT met: prompts WER after the full mouse 5 pipeline: 22.4% (68 of 304 words ...)`. Corra também `.venv\Scripts\python -m bench.asr_errors --check docs\research\asr-errors-summary.json`: deve dizer `ok` nos 4 conjuntos.
2. Ligue o Quill como de costume (`.venv\Scripts\python -m quill`). Em `local\logs\quill.log`, a linha `Quill started` diz `final pass off`. Dite com o botão 5 para o Claude Code: o texto e o tempo são os de antes.
3. Para medir outra vez com a sua voz (alguns minutos, usa a placa gráfica e o Ollama), corra `.venv\Scripts\python -m bench.prompts --variants --safety --pass-candidates all` e peça ao Claude Code para pôr os números novos ao lado dos deste relatório.

## Decisões para o Sponsor

1. **A meta de 12 % nos prompts não foi cumprida (fica em 22,37 %).** Recomendação: a próxima opção grátis é gravar mais prompts para o Claude Code (com `py -3.12 -m bench.record --set prompts`, depois de o Claude Code acrescentar frases ao guião de prompts). Com só 15 prompts, as diferenças entre configurações (1 a 5 erros em 304) ficam dentro do acaso. Com mais gravações dá para ver se a passagem com o large-v3 e o filtro de voz (a que menos erra) ganha de facto, e se vale a pena juntar o texto dela com os termos do streaming. Custo 0 €, nada a instalar. Mesmo assim, nada do que foi medido aproxima os prompts dos 12 %.
2. **Passagem final desligada.** Recomendação: mantê-la desligada, porque nenhuma configuração melhorou os prompts sem errar mais termos. Se a quiser experimentar no dia a dia, veja «Passagem final do botão 5» no [guia de uso](../USAR.md): com o large-v3 e beam 10, a passagem demorou 0,759 s a meio da lista e até 1,026 s, e do largar ao texto até 5,587 s. Custo 0 €.
3. **Nenhum modelo novo a descarregar.** O diagnóstico não mostra nenhum ganho de mudar de modelo (o large-v3 e o turbo, já no disco, diferem em 1 a 2 erros), por isso não há nenhum comando de descarga para aprovar.
4. **Continuam abertas as decisões das fases anteriores**, incluindo a do motor e do custo em [ENGINES.md](ENGINES.md) e as das Fases 7 a 9 ([FASE7.md](FASE7.md), [FASE8.md](FASE8.md), [FASE9.md](FASE9.md)).
