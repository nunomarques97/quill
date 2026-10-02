# Quill: botão 5 no Claude Code com o contexto do projeto (Fase 6)

**Estado: medido, 2026-09-30.** As 15 gravações do guião de prompts (4 projetos) e os 9 ditados para o Claude Code que já existiam foram medidos com a sua voz. No botão 5 novo, nenhuma palavra perdida e nenhuma inventada (metas cumpridas). A meta dos termos do domínio (com o contexto do projeto, no máximo metade dos erros de hoje) **não foi cumprida**: 5 erros contra 7. Fica para decisão sua (em baixo); a meta não foi baixada.

O resumo com só números está em [prompts-summary.json](prompts-summary.json). O texto de cada gravação, o que o botão 5 escreveria hoje e com a mudança, e os exemplos antes e depois do enriquecimento ficam em `bench/results/prompts/20260930-185820/` (ignorado pelo Git).

## O que mudou antes desta medição

A medição parcial anterior (só os 9 ditados) deixou duas recomendações, aplicadas antes de medir, sem mexer na verificação do enriquecimento nem nas metas:

1. **Instruções do enriquecimento:** usar só as partes que o ditado diz (o «Pedido» sempre, as outras só quando foram ditas), copiar cada frase uma só vez, nunca repetir o ditado noutra parte, nunca escrever uma parte a dizer que nada foi dito, e quatro exemplos curtos inventados, a maioria sem restrições nem critérios.
2. **Correção só com termos, só no botão 5 no Claude Code:** aí, uma troca só pode pôr uma palavra que não foi dita quando essa palavra é um termo do vocabulário pessoal, um termo do pacote do projeto ou o nome do projeto. Maiúsculas e acentos corrigidos ficam sempre. Qualquer outra troca é desfeita: essa palavra fica como foi ouvida e as outras correções ficam. O botão 5 nas outras janelas e a revisão automática dos ditados longos continuam como antes.

As instruções do enriquecimento foram afinadas só com os textos dos 9 ditados antigos; as 15 gravações novas só foram usadas nesta medição.

## Como foi medido

- Comando: `.venv\Scripts\python -m bench.prompts --require` (os dois conjuntos).
- Cada gravação passou pelo mesmo caminho da aplicação: transcrição em streaming no PC com o large-v3-turbo e as dicas da aplicação, o pipeline de texto da aplicação (limpeza, vocabulário, correções aprendidas, perfil e deteção do projeto), a janela simulada da caixa do Claude Code no VS Code com o título no formato da Central de Projetos, a pasta do projeto pelos atalhos e pela lista local, e o pacote de contexto guardado em `local/`.
- O botão 5 correu duas vezes em cada gravação, com o qwen3:8b no Ollama partilhado (nenhum modelo foi descarregado, nada foi instalado, 0 €):
  - **hoje:** a correção do botão 5 como era antes desta fase: a pista do título da janela, sem pacote, sem a regra «só termos» e sem enriquecimento;
  - **novo:** o projeto detetado, o seu pacote, a correção com contexto (com a regra «só termos») e o enriquecimento do prompt.
- Na medição, cada chamada ao modelo teve até 120 s para as contagens não dependerem da carga do PC; as chamadas que passariam os limites da aplicação (4 s na correção, 15 s no enriquecimento) são contadas à parte: nenhuma passou. Com os limites da aplicação e o modelo fora da memória, as correções acabavam todas aos 4 s; a causa e a correção estão em [LATENCIA-CORRECAO.md](LATENCIA-CORRECAO.md) (Fase 7).
- Nada foi escrito em nenhuma janela e nada foi enviado para o Claude Code.

## Resultados

Prompts gravados: 15 gravações, projeto detetado e pacote de contexto em todas, 16 ocorrências de termos do domínio. Ditados antigos: 9 gravações, projeto detetado em 4 (as outras 5 não dizem o nome de nenhum projeto).

| Medição | Hoje | Novo | Meta | Cumprida |
|---|---|---|---|---|
| Erros em termos do domínio (prompts; 7 já depois do pipeline de texto) | 7 de 16 | 5 de 16 | no máximo metade de hoje (3) | **não** |
| Palavras de conteúdo perdidas (24 gravações) | 1 | 0 | 0 | sim |
| Palavras de conteúdo inventadas (24 gravações, contagem estrita) | 4 | 0 | 0 | sim |
| Palavras do pacote fora da parte «Contexto» | — | 0 | 0 | sim |
| Prompts enriquecidos (prompts) | — | 3 de 13 pedidos | — | — |
| Prompts enriquecidos (ditados antigos) | — | 5 de 8 pedidos | — | — |

Na medição parcial anterior (antes das duas mudanças), nos 9 ditados antigos, o botão 5 novo inventou 3 palavras e enriqueceu 0 de 8 prompts.

Correção de hoje nos prompts: 2 textos corrigidos e 13 recusados pela verificação (foi o ditado). Correção nova nos prompts: 4 textos corrigidos, 9 sem mudanças, 2 recusados pela verificação (foi o ditado). Enriquecimento recusado em 13 dos 21 pedidos: 7 pelo comprimento, 3 por partes vazias, 2 por palavras do projeto fora do «Contexto» e 1 por uma palavra inventada; em todos foi enviado o texto corrigido.

Latência, em segundos (p50 / p95):

| Etapa | Prompts | Ditados antigos |
|---|---|---|
| Transcrição (do fim da fala ao texto) | 0,42 / 0,47 | 0,39 / 0,54 |
| Correção de hoje | 3,32 / 3,86 | 1,21 / 3,37 |
| Pacote de contexto (da cache) | 0,10 / 0,14 | 0,09 / 0,10 |
| Correção nova | 0,99 / 1,48 | 1,24 / 1,62 |
| Enriquecimento | 3,27 / 7,91 | 1,54 / 7,29 |
| Botão 5 novo, total (pacote, correção e enriquecimento) | 4,22 / 8,89 | 2,66 / 8,77 |

As mesmas instruções do botão 5 novo já tinham sido medidas duas vezes antes desta medição, com as mesmas contagens do botão 5 novo. Só a latência mudou: o total nos prompts foi 2,32 / 4,28 s na primeira, 3,79 / 8,15 s na segunda e 4,22 / 8,89 s nesta, o que aponta para outro trabalho na placa gráfica partilhada. Nessas duas medições, a coluna «hoje» já trazia a regra «só termos» e não mostrava a correção de hoje; esta medição substitui-as.

## O que os números mostram

1. **Nada se perdeu e nada foi inventado no botão 5 novo.** A correção de hoje perdeu 1 palavra e inventou 4 (contagem estrita): 2 dessas 4 são palavras que tinha dito e que o Whisper ouviu mal (a correção de hoje repô-las); as outras 2 são formas de verbo que não disse. No Claude Code, a regra «só termos» tira estas trocas. O custo: uma palavra comum mal ouvida (por exemplo, a forma certa de um verbo) deixa de ser corrigida no Claude Code; fica como o Whisper a ouviu.
2. **Termos do domínio: 7 para 5, a meta pedia 3.** Os 7 erros já vêm da transcrição (nem o pipeline de texto nem a correção de hoje corrigem nenhum). A correção com contexto corrigiu 2. Nos 5 que ficaram (lidos um a um nos ficheiros ignorados):
   - em 2, a troca parecia possível (o termo ouvido como uma palavra comum parecida; o termo ouvido como duas palavras comuns), mas o modelo deixou o texto como estava;
   - em 3, a verificação não deixa fazer a troca, de propósito: o Whisper escreveu no lugar do termo outro nome com maiúsculas (1), pedaços em maiúsculas como siglas (1) ou um número junto com a palavra seguinte (1). A verificação protege nomes e números para a correção nunca os mudar.
3. **Enriquecimento: 8 de 21 aceites (antes, 0 de 8).** Quando passa, o prompt fica com «Pedido» e, às vezes, «Objetivo» ou «Contexto». Nas respostas vistas ao afinar as instruções (ditados antigos), o modelo ainda repetia, por vezes, o ditado em duas partes, copiava um resumo comprido para o «Contexto» ou copiava palavras dos exemplos; a verificação recusou essas respostas. Nota sobre o «Contexto»: o resumo do pacote é o primeiro parágrafo do `CLAUDE.md` do projeto; em 2 dos 4 projetos esse parágrafo são instruções para agentes, em inglês, e não uma descrição do projeto, e o modelo copia-o tal como está para o «Contexto».
4. **Latência:** o botão 5 novo demorou, a meio da lista, entre 2,7 e 4,2 s depois da transcrição (p95 até 8,9 s), com a placa gráfica partilhada ocupada; numa medição anterior das mesmas instruções, entre 2,0 e 2,3 s (p95 até 5,8 s). A correção de hoje, nos mesmos prompts, demorou 3,3 s a meio da lista. Nenhuma chamada passou os limites da aplicação (4 s na correção, 15 s no enriquecimento).

## Decisões para o Sponsor

1. **Meta dos termos do domínio não cumprida (5 contra 7; meta 3).** A meta não foi baixada. Recomendação: numa próxima fase, dar ao Whisper os termos do pacote do projeto detetado como dicas no botão 5, porque os erros nascem na transcrição, e medir outra vez nestas mesmas 15 gravações. Custo 0 €, nada a instalar. Nesta fase ficou de fora de propósito, porque muda a transcrição que estava a ser medida.
2. **Enriquecimento aceite em 8 de 21.** Leia `bench/results/prompts/20260930-185820/exemplos.md` e escreva `sim` ou `não` por baixo de cada prompt enriquecido. Recomendação: manter o enriquecimento ligado, porque nunca envia pior do que o texto corrigido e custa cerca de 1,5 a 3,3 s a meio da lista. Se os prompts não o convencerem, a alternativa é desligá-lo e ficar só com a correção com contexto.
3. **Resumo do projeto que são instruções para agentes.** Recomendação: quando o `CLAUDE.md` começa por instruções, usar a descrição do `README.md` para o resumo do pacote. Custo 0 €. Até lá, o «Contexto» desses 2 projetos traz texto que não descreve o projeto.
4. **Regra «só termos» nas outras janelas.** Hoje só se aplica ao botão 5 no Claude Code; nas outras janelas o botão 5 corrige como antes. Recomendação: deixar assim, como foi pedido para esta fase. Se a quiser também nas outras janelas, é uma decisão sua: evita palavras que não disse, mas deixa de corrigir palavras comuns mal ouvidas (nesta medição, 2 das 4 trocas da correção de hoje repunham palavras que tinha dito).

Os ficheiros das medições anteriores (`bench/results/prompts/20260930-133202/` parcial, `20260930-183409/` e `20260930-184435/`, com a coluna «hoje» já com a regra «só termos») ficam como histórico. Esta medição (`20260930-185820/`) substitui-as.
