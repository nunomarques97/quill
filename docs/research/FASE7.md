# Quill: relatório da Fase 7 (botão 5 no Claude Code)

**Estado: concluída a 2026-10-03, sem o Sponsor presente.** Este documento tem só números agregados: nenhum texto falado, nenhum nome de projeto e nenhum caminho da máquina. O texto de cada gravação fica em `bench/results/` (ignorado pelo Git). Tudo correu no PC (Whisper e o Ollama partilhado), sem instalar nada, sem tirar nem apagar modelos de outros projetos e com custo 0 €.

## O que estava errado com o botão 5 na extensão do Claude Code

No registo local de 2026-10-02, todos os ditados com o botão 5 para o Claude Code no VS Code ficaram como «perfil vscode»: o texto era escrito, mas sem Enter, sem o projeto e sem o prompt enriquecido. Havia três causas:

1. **Claude Code num separador do editor (a causa principal).** Nas janelas verificadas, a conversa do Claude Code estava quase sempre aberta como separador do editor. Aí o título do VS Code acaba em `[]` (vazio), como o de qualquer outra janela, e o separador muda de nome para o título da conversa. Pelo título, o Quill não tinha forma de saber que era o Claude Code e, por segurança, não carregava em Enter.
2. **Projeto sem ficheiro aberto.** No formato da Central de Projetos sem nenhum ficheiro aberto («<projeto> | - Visual Studio Code»), o Quill não lia o projeto e registava `no_name`.
3. **Estado do ficheiro no fim do título.** Com o modo para leitores de ecrã ligado neste PC (`"editor.accessibilitySupport": "on"`), o VS Code acrescenta ao fim do título o estado do ficheiro ativo (por exemplo, «- Modified»), depois da marca `[Claude Code]`, e o Quill deixava de a reconhecer. Isto não foi visto nas janelas abertas durante a verificação; ficou corrigido na mesma.

Na mesma noite, várias correções acabaram aos 4,0 s porque o modelo local não estava em memória, e um segundo toque no botão 5 durante o primeiro ditado abriu uma sessão que falhou.

## O que mudou (T1 a T6)

| Tarefa | O que mudou | Números |
|---|---|---|
| T1: título do VS Code | A marca `[Claude Code]` é reconhecida logo a seguir a «Visual Studio Code», com ou sem o estado do ficheiro no fim; o projeto é lido também sem ficheiro aberto; antes do Enter, o Quill volta a confirmar que a janela ativa ainda é o Claude Code e que o título não ganhou o sinal de ficheiro alterado. Novo diagnóstico só de leitura: `py -3.12 -m quill.profiles --probe`. | Testes com títulos inventados e janelas simuladas. |
| T2: separador do editor | Quando o título não chega, o Quill pergunta ao Windows (UI Automation, sem instalar nada) que elemento tem o foco. Só a caixa de texto do Claude Code conta como Claude Code; um ficheiro, o terminal integrado ou outra página ficam como antes, sem Enter. Resposta em no máximo 0,8 s; sem resposta, sem Enter. Volta a perguntar logo antes do Enter. Shift+Enter entre linhas, um só Enter no fim (ou Ctrl+Enter, se o configurar). | Testes com árvores de elementos simuladas; nunca lê o texto, nunca muda o foco. |
| T3: toques sobrepostos | Enquanto um ditado ainda está a ser tratado, um toque em qualquer botão ou tecla do Quill não faz nada (não clica, não abre o microfone) e o indicador mostra «Aguarde: o ditado anterior ainda está a ser escrito» durante 2 s. Igual no botão 4, no botão 5 e no botão do meio. | Testes com o duplo toque do registo e com todos os gatilhos. |
| T4: correções aos 4,0 s | O Quill carrega o seu modelo em segundo plano (ao ligar, quando o Ollama não tem o modelo de outro projeto, e ao começar a premir o botão 5), espera no máximo 8 s pelo carregamento e só depois dá 4 s ao modelo. `keep_alive` de 30 minutos só nos pedidos do Quill. Detalhes em [LATENCIA-CORRECAO.md](LATENCIA-CORRECAO.md). | Correções que acabaram por tempo: 48 de 48 antes, 0 de 48 depois. Correção p50/p95: 0,67/0,92 s (prompts) e 0,89/1,04 s (ditados). |
| T5: termos do projeto como dicas | Com o botão 5 no Claude Code e o projeto reconhecido, o Whisper ouve o resto do ditado com o nome do projeto e os termos mais relevantes do pacote como dicas. Detalhes em [PROMPTS.md](PROMPTS.md). | Erros em termos do domínio no texto final: 7 de 16 (correção de antes), 5 (sem as dicas), 4 (com as dicas). Meta 3: **não cumprida**. |
| T6: enriquecimento | O resumo do projeto deixa de usar instruções para agentes; o modelo só vê a primeira frase do resumo; um passo fixo tira da resposta só o que as instruções proíbem (cópia repetida, parte vazia, frase repetida), antes da verificação, que não mudou. Detalhes em [PROMPTS.md](PROMPTS.md). | Prompts enriquecidos: 20 de 20 pedidos, contra 8 de 19 antes (8 de 21 na Fase 6). |

## Números medidos com a sua voz

Medidos com `.venv\Scripts\python -m bench.prompts` nas gravações que já existiam: os 15 prompts gravados para 4 projetos e os 9 ditados para o Claude Code. O resumo só com números está em [prompts-summary.json](prompts-summary.json) e [correction-latency-summary.json](correction-latency-summary.json).

| Medição | Antes da Fase 7 | Depois | Meta | Cumprida |
|---|---|---|---|---|
| Correções que acabaram por tempo (limites da aplicação, modelo fora da memória no início) | 48 de 48 | 0 de 48 | — | — |
| Prompts enriquecidos | 8 de 21 (Fase 6) | 20 de 20 | — | — |
| Palavras de conteúdo perdidas (24 gravações) | 0 | 0 | 0 | sim |
| Palavras de conteúdo inventadas (24 gravações) | 0 | 0 | 0 | sim |
| Palavras do projeto fora do «Contexto» | 0 | 0 | 0 | sim |
| Erros em termos do domínio no texto final (prompts) | 5 de 16 (7 com a correção de antes) | 4 de 16 | no máximo 3 | **não** |
| Botão 5 depois da transcrição, prompts (p50 / p95) | 4,22 / 8,89 s (Fase 6) | 1,72 / 2,24 s | — | — |
| Enriquecimento, prompts (p50 / p95) | 3,27 / 7,91 s (Fase 6) | 1,08 / 1,74 s | — | — |
| Transcrição, prompts (p50 / p95) | 0,42 / 0,47 s (Fase 6) | 0,23 / 0,25 s | — | — |

As medições de antes e de depois correram em alturas diferentes na placa gráfica partilhada; a latência depende também do que os outros programas estão a fazer.

A deteção do Claude Code (T1 e T2) e os toques sobrepostos (T3) foram verificados com testes com janelas, títulos e elementos simulados: os testes nunca usam o rato, o teclado, o foco ou o ecrã reais. Falta a confirmação no seu PC (em baixo).

## Como verificar de manhã

1. Abra o Terminal do Windows na pasta do Quill, escreva `py -3.12 -m quill.profiles --probe --delay 5` e carregue em Enter. Nos 5 segundos seguintes, clique na caixa de texto de uma conversa do Claude Code no VS Code (no separador do editor, onde costuma usá-la). Volte ao terminal: a linha deve dizer `profile claude-code` e `mouse 5 Enter yes`.
2. Ligue o Quill como de costume (`.venv\Scripts\python -m quill`), clique na caixa do Claude Code, mantenha premido o botão 5, diga um pedido com mais de 6 palavras e largue. O texto deve aparecer organizado («Pedido», e às vezes «Objetivo» ou «Contexto»), o Quill deve carregar uma vez em Enter e o indicador deve mostrar «Prompt enriquecido e enviado».
3. Abra um ficheiro qualquer no mesmo VS Code, clique dentro dele e repita o botão 5 com uma frase curta. O texto deve ser escrito sem Enter. Se carregar outra vez no botão 5 enquanto o primeiro texto ainda está a ser escrito, o indicador deve mostrar «Aguarde: o ditado anterior ainda está a ser escrito».

Se o passo 1 disser `focus unavailable`, a acessibilidade do VS Code está desligada: veja «Claude Code num separador do editor» em [../USAR.md](../USAR.md).

## Gravações novas

Nenhuma. Todas as medições usaram as gravações que já existiam. A recomendação abaixo para os termos do domínio pode ser medida nas mesmas 15 gravações.

## Decisões para o Sponsor

1. **Meta dos termos do domínio não cumprida (4 contra 7; meta 3).** A meta não foi baixada. Recomendação: escolher os termos do pacote que entram nas dicas pelo que já foi ouvido no ditado (os termos que soam parecido com as palavras da transcrição parcial), em vez de só pela relevância do pacote, e medir outra vez nas mesmas 15 gravações. Custo 0 €, nada a instalar.
2. **Leia os prompts enriquecidos.** Abra `bench/results/prompts/20261003-012518/exemplos.md` e escreva `sim` ou `não` por baixo de cada um. Em cerca de 4 dos 20, as palavras estão todas lá mas numa parte que não parece a certa; a verificação aceita-os porque não perdem nem inventam nada. Recomendação: manter o enriquecimento ligado.
3. **O separador do editor depende da acessibilidade do VS Code.** Funciona porque `"editor.accessibilitySupport": "on"` já está ligado neste PC. Recomendação: deixar ligado. Se o desligar, a barra lateral com a marca `[Claude Code]` continua a enviar, mas o separador do editor passa a ser escrito sem Enter. O Quill não muda as definições do VS Code.
