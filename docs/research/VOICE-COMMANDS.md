# Quill: próximos comandos de voz

**Estado: estudo, 2026-09-30.** Nada do que está descrito na secção [Cenários](#cenários) está implementado. Este documento serve para o Sponsor escolher o próximo conjunto de comandos; cada um precisa de uma decisão antes de ser construído.

Os nomes usados nos exemplos são inventados (`<projeto>`, `alfa`, `alfa-public`, `<contacto>`). Os nomes reais dos projetos e das pessoas ficam só nos ficheiros ignorados (`local/`).

## O que já existe

- **Botão 4 do rato (ditado):** escreve o texto limpo onde está o ponteiro. Nunca carrega em Enter.
- **Botão 5 do rato (`send_polished`):** o texto passa sempre pela revisão do modelo local (Ollama), seja qual for o comprimento, com a guarda de conteúdo e o texto original como alternativa se o modelo falhar; é escrito e segue-se um Enter simples.
- **Botão do meio (`send_raw`):** escreve o texto tal como foi dito, sem revisão, e carrega em Enter.
- **Enter só no Claude Code:** o Enter dos botões 5 e do meio só é carregado no Claude Code (Terminal do Windows ou a caixa do Claude Code na barra lateral do VS Code, com o marcador `[Claude Code]` no fim do título). Num editor de código, no terminal integrado do VS Code, no browser ou no WhatsApp, o texto fica escrito sem Enter.
- **F9 (comandos de voz):** mantém-se a tecla premida, diz-se «abre VS Code no `<projeto>`» e larga-se. O Quill lista os atalhos `.lnk` das pastas de `[voice_commands] shortcut_dirs`, compara o nome dito com os nomes dos atalhos (vocabulário pessoal e distância de edição limitada; «alfa» abre alfa e «alfa public» abre alfa-public) e abre o único atalho claro. Se houver dúvida, não abre nada e mostra os nomes mais próximos no indicador. A F9 nunca clica, nunca escreve e nunca carrega em Enter; o texto dito nunca chega a uma shell.

O analisador (`quill/voice.py`, classe `Parser`) é um registo: cada comando novo é uma classe com `parse(text)` e `run(intent)` e entra com `register`, sem mexer nos outros. Os cenários abaixo assumem esse caminho.

A medição dos comandos de voz com a voz real do Sponsor (meta: pelo menos 95 % de ações certas e 0 atalhos errados abertos) está pendente das gravações. **Recomendação geral: não acrescentar comandos antes de essa meta estar cumprida no comando que já existe**, porque todos os cenários dependem do mesmo reconhecimento de nomes.

## Como ler cada cenário

- **Valor:** o que poupa no dia a dia de quem usa Claude Code, VS Code, browser, WhatsApp e FORJA.
- **Risco:** ação errada, privacidade, efeitos irreversíveis, áudio ou dados que saem do PC, serviços pagos.
- **Esforço:** pequeno (um comando novo sobre módulos existentes, poucos dias), médio (um módulo novo ou uma API do Windows nova, com testes) ou grande (novo tipo de interação, decisão do Sponsor sobre custo ou segurança). Indica os módulos em que se apoia.

Em todos os cenários o áudio continua a ser transcrito só no PC (large-v3-turbo local). Nenhum usa um serviço pago nem um modelo de voz na nuvem.

## Cenários

### 1. Abrir o projeto no GitHub ou no browser

Exemplo: «abre o GitHub do `<projeto>`», «abre o site do `<projeto>`».

- **Valor:** alto. Ir ao repositório, às issues ou ao pull request é diário e hoje exige procurar o separador ou escrever o endereço.
- **Risco:** baixo a médio. Um nome mal reconhecido abre a página errada (sem efeitos, fecha-se o separador). O endereço vem do `remote` do Git da pasta do projeto, lido só para leitura, e só é aberto se for `https://github.com/...` ou um endereço listado no `local/quill.toml`; o texto dito nunca vira endereço. O pedido à página sai do PC, como qualquer visita no browser, mas o áudio não. Repositórios privados abrem só com a sessão já iniciada no browser.
- **Esforço:** pequeno. Reutiliza `quill/shortcuts.py` (lista e alvo das pastas dos projetos, `match`), `quill/voice.py` (novo comando no `Parser`) e o vocabulário pessoal; falta ler `.git/config` e abrir o endereço com o shell, com uma lista de hosts permitidos.

### 2. Mudar de janela

Exemplo: «vai para o WhatsApp», «muda para o VS Code do `<projeto>`», «vai para o Claude».

- **Valor:** alto. Troca de contexto constante entre Claude Code, VS Code, browser e WhatsApp, sem Alt+Tab às cegas.
- **Risco:** médio. Trazer a janela errada para a frente não destrói nada, mas o próximo ditado com o botão 4 cairia nela se o ponteiro estivesse lá (o clique para focar reduz este risco). Os títulos das janelas têm dados privados (nomes de ficheiros, conversas): só podem ser comparados em memória, nunca registados nos logs. O Windows limita quem pode trazer uma janela para a frente; se recusar, o comando tem de dizer "Não consegui mudar de janela" em vez de fingir que mudou.
- **Esforço:** médio. Apoia-se em `quill/profiles.py` (`WindowInfo`, processo, classe e título, já usados para os perfis), `quill/win32.py` (`User32`) e no `match` de `quill/shortcuts.py`. Falta enumerar as janelas visíveis e pedir o primeiro plano, com testes que usam uma API falsa e nunca mexem no ecrã real.

### 3. Iniciar uma execução da FORJA

Exemplo: «FORJA, começa no `<projeto>`: <objetivo>».

- **Valor:** médio. Poupa abrir o terminal e escrever o comando, mas o objetivo de uma execução é longo e importante e fica melhor revisto por escrito.
- **Risco:** alto. Uma execução gasta a subscrição ou o crédito do fornecedor (custo possivelmente pago ou desconhecido, que precisa de decisão do Sponsor), trabalha durante muito tempo, altera ficheiros e pode entregar commits. Um objetivo mal transcrito ou o projeto errado dão trabalho feito no sítio errado. O texto do objetivo sai do PC para o fornecedor do modelo, como já acontece quando se escreve no Claude Code.
- **Esforço:** grande se lançar a execução; pequeno numa versão segura: o comando abre o terminal na pasta do projeto certo (pelos mesmos atalhos) com o comando `forja start` preparado e o objetivo ditado **sem Enter**, para o Sponsor rever e confirmar. Apoia-se em `quill/shortcuts.py`, `quill/voice.py` e `quill/inject.py` (que nunca carrega em Enter sozinho). **Recomendação: só a versão que prepara, nunca a que lança.**

### 4. Ver o estado de uma execução da FORJA

Exemplo: «como está a FORJA no `<projeto>`?».

- **Valor:** alto. Saber se a execução está a correr, parada à espera de uma decisão ou acabada, sem abrir o terminal.
- **Risco:** baixo. Só lê o estado (`forja status`, ou os ficheiros de estado em `.forja/` só para leitura) e mostra uma linha curta no indicador, como "FORJA: a desenvolver, tarefa 3 de 4" ou "FORJA: à espera do Sponsor". Nada é alterado e nada sai do PC. O caminho da instalação da FORJA fica só no `local/quill.toml`. Não pode retomar, parar nem decidir nada.
- **Esforço:** pequeno a médio. Reutiliza `quill/voice.py`, o `match` de projetos e os estados do indicador (`quill/indicator`); falta ler o estado da FORJA com um processo filho com lista de argumentos e tempo limite, e um formato de estado estável da FORJA. Combina bem com o aviso de atenção do Claude Code (`quill/notify.py`), que já avisa quando o Claude Code termina.

### 5. Colar o último ditado noutro sítio

Exemplo: aponta-se para outro campo e diz-se «cola o último ditado» (ou «repete aqui»).

- **Valor:** alto. Acontece ditar para a janela errada, ou querer o mesmo texto no Claude Code e no WhatsApp.
- **Risco:** médio. O último texto tem de ficar guardado, só em memória e por pouco tempo (por exemplo 10 minutos), nunca em disco nem nos logs. O texto é escrito como no ditado (sem clipboard e sem Enter, `quill/inject.py`), por isso não é enviado sozinho; o risco é escrever num campo errado, que se desfaz com Ctrl+Z.
- **Esforço:** pequeno. Reutiliza a sessão (`quill/session.py`, que já sabe quando um texto foi escrito), `quill/focus.py` (clique no sítio apontado; seria o primeiro comando de voz a clicar, o que tem de ser decidido) e `quill/inject.py`. Alternativa sem voz: uma tecla dedicada, ainda mais simples.

### 6. Abrir o Claude Code no projeto

Exemplo: «abre o Claude no `<projeto>`».

- **Valor:** alto. É a outra forma diária de começar a trabalhar num projeto, ao lado do VS Code.
- **Risco:** baixo a médio. Abre o Terminal do Windows na pasta do projeto certo e inicia `claude` com uma lista de argumentos (nunca uma linha de shell com texto dito). Um projeto errado abre uma sessão no sítio errado, que se fecha sem efeitos. Não escreve nem envia nada.
- **Esforço:** pequeno. É o comando que já existe com outro destino: `quill/shortcuts.py` (pasta alvo do atalho, já validada) e `quill/voice.py`; falta a regra de lançamento do terminal e os testes com um lançador falso.

### 7. Abrir a pasta do projeto no Explorador

Exemplo: «mostra os ficheiros do `<projeto>` no Explorador».

- **Valor:** médio. Útil para arrastar ficheiros para o WhatsApp ou para o browser.
- **Risco:** baixo. Só abre uma pasta que já é o alvo de um atalho validado; nada é alterado.
- **Esforço:** pequeno. `quill/shortcuts.py` e `quill/voice.py`, com a pasta aberta pelo Explorador com uma lista de argumentos.

### 8. Abrir uma conversa do WhatsApp

Exemplo: «abre o WhatsApp da `<contacto>`».

- **Valor:** médio. Poupa procurar a conversa antes de ditar uma mensagem.
- **Risco:** alto em privacidade. Precisa de uma lista de contactos no `local/` (nomes de pessoas, nunca no repositório) e o WhatsApp não tem uma forma local e estável de abrir uma conversa por nome; o caminho documentado usa o número de telefone. Abrir a conversa errada e depois ditar com Enter manual pode mandar uma mensagem à pessoa errada, e uma mensagem enviada não se desfaz. O Quill continua a nunca carregar em Enter fora do Claude Code.
- **Esforço:** médio a grande. Reutiliza `quill/voice.py`, o `match` e o vocabulário; falta a lista privada de contactos e confirmar como o WhatsApp para Windows abre uma conversa. **Recomendação: adiar; o ganho é pequeno para o risco.**

### 9. Estado do Git do projeto

Exemplo: «há alterações no `<projeto>`?».

- **Valor:** médio. Uma resposta curta no indicador ("3 ficheiros alterados, 1 commit por enviar") antes de mudar de tarefa.
- **Risco:** baixo. Só leitura (`git status` com lista de argumentos e tempo limite). Nomes de ficheiros não aparecem no indicador nem nos logs, só contagens. Nada sai do PC (não faz `fetch`).
- **Esforço:** pequeno. `quill/shortcuts.py` (pasta do projeto), `quill/voice.py` e os estados do indicador.

### 10. Pesquisar na web

Exemplo: «pesquisa <termos>».

- **Valor:** médio. Rápido para dúvidas técnicas.
- **Risco:** médio. Ao contrário dos outros, **o texto dito sai do PC** para o motor de pesquisa (o áudio não). Uma transcrição errada pesquisa outra coisa, sem outros efeitos. O texto dito vai só codificado dentro do endereço de um motor fixo no `local/quill.toml`, nunca como endereço livre.
- **Esforço:** pequeno. `quill/voice.py` e o mesmo lançador de endereços do cenário 1.

## Resumo

| # | Cenário | Valor | Risco | Esforço |
|---|---|---|---|---|
| 1 | Abrir o projeto no GitHub ou no browser | alto | baixo a médio | pequeno |
| 2 | Mudar de janela | alto | médio | médio |
| 3 | Iniciar uma execução da FORJA | médio | alto (custo, ficheiros, commits) | grande; pequeno se só preparar |
| 4 | Ver o estado de uma execução da FORJA | alto | baixo | pequeno a médio |
| 5 | Colar o último ditado noutro sítio | alto | médio | pequeno |
| 6 | Abrir o Claude Code no projeto | alto | baixo a médio | pequeno |
| 7 | Abrir a pasta do projeto no Explorador | médio | baixo | pequeno |
| 8 | Abrir uma conversa do WhatsApp | médio | alto (privacidade, mensagem à pessoa errada) | médio a grande |
| 9 | Estado do Git do projeto | médio | baixo | pequeno |
| 10 | Pesquisar na web | médio | médio (texto sai do PC) | pequeno |

## Conjunto recomendado a seguir

Depois de a medição do «abre VS Code no `<projeto>`» cumprir a meta (pelo menos 95 % de ações certas e 0 atalhos errados):

1. **Abrir o Claude Code no projeto (6)** e **abrir o projeto no GitHub (1)**: usam o mesmo reconhecimento de nomes e a mesma lista de atalhos que já foram medidos, por isso o risco de abrir a coisa errada já está medido; são só leitura ou abertura, sem efeitos irreversíveis, e cobrem as duas coisas que se fazem ao começar a trabalhar num projeto.
2. **Ver o estado de uma execução da FORJA (4):** valor alto, só leitura, nada sai do PC, e completa o aviso do Claude Code que já existe.
3. **Colar o último ditado (5):** resolve o erro mais comum do ditado (janela errada) com pouco código; o texto fica só em memória e nunca é enviado sozinho.

Ficam para depois: **mudar de janela (2)**, porque precisa de uma API nova do Windows e de lidar com as recusas de primeiro plano; **iniciar a FORJA (3)**, só na versão que prepara o comando sem Enter e depois de uma decisão do Sponsor sobre o custo; **Git (9)**, **Explorador (7)** e **pesquisa (10)**, de valor menor; **WhatsApp (8)**, pelo risco de privacidade e de mensagens à pessoa errada.

Cada comando novo deve entrar com frases de teste inventadas no guião de comandos e ser medido na voz real com as mesmas metas antes de ficar ligado por omissão.
