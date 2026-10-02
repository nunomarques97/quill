# Quill: correções do botão 5 que acabavam aos 4,0 s (Fase 7)

**Estado: medido, 2026-10-02.** A causa foi o modelo local (qwen3:8b) não estar em memória quando o botão 5 pedia a correção. Carregá-lo demora mais do que os 4 s que o Quill esperava, e ao desistir o Quill fazia o Ollama abortar o carregamento. Por isso o ditado seguinte voltava a encontrar o modelo fora da memória. Com a mudança, nas mesmas gravações: **0 correções acabaram por tempo, contra 48 de 48 antes**.

Só números estão em [correction-latency-summary.json](correction-latency-summary.json). O texto de cada gravação fica em `bench/results/prompts/` (ignorado pelo Git).

## O que mostrava o registo

- No registo local do Quill houve 15 correções do botão 5: 10 acabaram por tempo, entre 4,00 e 4,02 s, e o ditado foi escrito sem correção. As outras 5 responderam em 0,53 a 0,74 s (uma em 3,64 s).
- As 10 que acabaram por tempo foram a primeira depois de ligar o Quill ou a primeira depois de mais de 5 minutos sem nenhum pedido do Quill ao Ollama.
- O registo do Ollama partilhado (só leitura) mostra que:
  - cada modelo fica em memória 5 minutos depois do último pedido (o valor do servidor);
  - só cabe um modelo de cada vez na placa gráfica, por isso o modelo de outro projeto tira o do Quill, e vice-versa;
  - carregar o qwen3:8b demorou entre 1,8 e 6,3 s (a meio da lista 4,5 s; em 95 % dos casos até 5,8 s), mais até cerca de 3 s para o Ollama verificar a placa gráfica antes de carregar;
  - quando o pedido desiste antes do fim, o Ollama aborta o carregamento («client connection closed before llama-server finished loading»): isto aconteceu 9 vezes.
- **O tamanho do prompt não é a causa.** Com o modelo já em memória, cada correção sem contexto do projeto tem cerca de 2 300 caracteres (566 tokens) e o servidor responde a meio da lista em 1,1 s. Com o contexto do projeto tem cerca de 4 800 caracteres (1 181 tokens) e responde em 0,7 s. O enriquecimento tem cerca de 5 200 caracteres (1 308 tokens) e responde em 1,8 s. O modelo também não muda: com ele em memória, tudo cabe nos 4 s.

## O que mudou

1. **O Quill carrega o seu modelo antes de precisar dele**, num fio de execução próprio, sem gerar texto (um pedido de chat vazio):
   - ao ligar, depois do modelo de voz, com a revisão automática ligada (`enabled = true`), mas só se o Ollama não tiver o modelo de outro projeto em memória (para não o tirar sem necessidade);
   - sempre que começa a premir o botão 5: o modelo carrega enquanto fala.
   Nunca há mais de um carregamento ao mesmo tempo. Este pedido espera até 60 s, por isso o carregamento já não é abortado. Uma falha fica só no registo.
2. **Antes da correção, o Quill espera pelo modelo no máximo `load_wait_s` segundos** (por omissão 8). Só depois dá ao modelo os 4 s de `timeout_s`. A correção acaba, ou escreve o ditado tal como foi dito, em no máximo `load_wait_s` + `timeout_s` segundos (12 s por omissão). Se o carregamento ainda não tiver terminado, continua para o ditado seguinte.
3. **`keep_alive` só nos pedidos do próprio Quill:** o Ollama mantém o modelo do Quill 30 minutos depois de cada pedido do Quill (`[autorewrite] keep_alive = "30m"`), em vez dos 5 minutos do servidor. O Quill só aceita durações positivas, de 1 minuto a 4 horas. Recusa 0, valores negativos, números sem unidade e qualquer valor que tire um modelo da memória. Nunca tira um modelo da memória, nunca apaga nem transfere modelos. Se outro projeto precisar da memória, o Ollama continua a dar-lha: tira um modelo parado, como antes.
4. Depois de uma correção que acabou por tempo ou falhou, o Quill volta a carregar o modelo em segundo plano, para o ditado seguinte.

## Como foi medido

- Comando: `.venv\Scripts\python -m bench.prompts --timeouts product` (os dois conjuntos: os 15 prompts gravados e os 9 ditados para o Claude Code), com os limites da aplicação (4 s na correção, 15 s no enriquecimento). Ao contrário da medição da Fase 6 (até 120 s por chamada, com uma chamada de aquecimento antes), aqui não houve aquecimento: o modelo ficou como estava.
- **Antes:** o código desta fase sem a mudança (sem carregar o modelo, sem esperar por ele, sem `keep_alive`). **Depois:** o código com a mudança. Em cada fase, o botão 5 é simulado no início de cada gravação, logo antes da correção; na aplicação, o carregamento começa quando começa a premir o botão e por isso já leva o tempo da fala de avanço.
- As duas medições começaram com o Ollama sem nenhum modelo em memória, porque o modelo de outro projeto saiu sozinho ao fim do seu tempo. Nenhum modelo foi descarregado pela medição, nada foi instalado, 0 €.
- Nada foi escrito em nenhuma janela e nada foi enviado para o Claude Code.

## Resultados

Cada gravação faz 2 correções: a de antes da Fase 6 e a nova, com o contexto do projeto. São 48 correções nas 24 gravações.

| Medição | Antes | Depois |
|---|---|---|
| Correções que acabaram por tempo (4 s) | 48 de 48 | 0 de 48 |
| Correções acima de 4 s | 48 de 48 | 1 de 48 (a primeira, com o modelo fora da memória: 8,4 s, respondida) |
| Primeira correção, com o modelo fora da memória | 4,0 s, acabou por tempo | 8,4 s: 7,5 s à espera do carregamento, depois 0,9 s de resposta |
| Correção nova, prompts (a meio da lista / 95 %) | 4,01 / 4,02 s | 0,67 / 0,92 s |
| Correção nova, ditados | 4,01 / 4,04 s | 0,89 / 1,04 s |
| Correção de antes da Fase 6, prompts | 4,01 / 4,04 s | 2,03 / 8,37 s (o 8,37 é a primeira) |
| Correção de antes da Fase 6, ditados | 4,01 / 4,03 s | 1,00 / 2,08 s |
| Botão 5 depois da transcrição, prompts | 4,18 / 4,79 s | 2,34 / 3,88 s |
| Botão 5 depois da transcrição, ditados | 4,03 / 4,17 s | 1,79 / 4,68 s |
| Prompts enriquecidos | 0 (nenhum chegou a ser pedido) | 6 de 20 pedidos |
| Palavras perdidas / inventadas (botão 5 novo) | 0 / 0 (nada foi corrigido) | 0 / 0 |
| Modelo em memória no fim | não | sim, por mais 30 minutos |

Antes, o modelo nunca chegou a ficar em memória durante a medição inteira: cada correção desistia aos 4 s e abortava o carregamento. É o mesmo que o registo mostra no seu uso real.

Os prompts enriquecidos (6 de 20) e os termos do domínio são o assunto das tarefas seguintes desta fase, e o relatório da fase dá os números finais. Esta medição só mostra que o enriquecimento agora chega a ser pedido.

## Limites

- Na aplicação, o carregamento começa quando começa a premir o botão 5. Num ditado de 3 s com o modelo fora da memória, a correção espera cerca de 4,5 s depois de largar o botão (7,5 s de carregamento menos 3 s de fala). Num ditado de 8 s ou mais, quase não espera.
- Se o carregamento demorar mais do que `load_wait_s`, a correção é pedida na mesma. Se o modelo não responder em `timeout_s`, o ditado é escrito tal como o disse, com o aviso de sempre. O carregamento continua e o ditado seguinte já o encontra em memória.
- Se outro projeto usar o Ollama entretanto, o modelo do Quill pode sair da memória antes dos 30 minutos. O próximo botão 5 volta a carregá-lo enquanto fala.
