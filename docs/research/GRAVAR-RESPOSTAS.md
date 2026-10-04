# Gravar as respostas ao Claude Code (Fase 12)

O objetivo é gravar a tua voz a dizer 10 respostas curtas ao Claude Code, como as que envias com o botão 5 do rato logo depois de o Claude te fazer uma pergunta ou te dar opções. Cada resposta fica emparelhada com uma mensagem real do Claude a que ela responde. Estas gravações servem para medir, com a tua voz real, se o rato 5 acerta mais nas palavras que repetes do Claude quando usa a última mensagem dele como contexto: sem nenhuma palavra perdida e sem nada inventado ou copiado da mensagem do Claude.

As frases a dizer estão no guião `bench/dictation/guiao-respostas-pt.md`. O guião publicado só tem `<projeto-N>` e `<termo-N>` e uma descrição genérica de cada mensagem do Claude. Os nomes reais dos projetos e as palavras reais do domínio vêm da configuração local `local/bench.toml` (secções `[replies.projects]` e `[replies.terms]`), que o Git ignora.

As mensagens reais do Claude e as gravações nunca saem do teu PC: as mensagens ficam em `local/replies/`, as gravações em `local/recordings/replies/` e os resultados com texto em `bench/results/`, três pastas que o Git ignora. Nada disto é enviado para a internet, para o Claude Code nem publicado. A medição só lê os ficheiros de `local/replies/`; nunca lê as conversas do Claude Code.

Tempo previsto: 20 a 30 minutos.

## Passos

1. Abre o VS Code na pasta do projeto `quill` e abre um terminal: menu **Terminal → New Terminal**.
2. Para cada linha do guião `bench/dictation/guiao-respostas-pt.md` (de `rr-01` a `rr-10`), procura numa conversa tua com o Claude Code uma mensagem real dele que corresponda à coluna `mensagem do Claude` (por exemplo, uma em que ele oferece três opções numeradas). Copia só o texto visível dessa mensagem.
3. Guarda cada mensagem copiada num ficheiro de texto simples dentro da pasta `local/replies/` do projeto, com o nome da linha: `local/replies/rr-01.md`, `local/replies/rr-02.md`, e assim por diante até `local/replies/rr-10.md`. Cria a pasta `local/replies` se ainda não existir. Cada ficheiro tem no máximo 256 KB; uma mensagem normal tem muito menos.
4. Abre `local/bench.toml` e acrescenta, no fim, as duas secções seguintes. Em `[replies.projects]` põe o nome do projeto (o nome do atalho no project hub) da janela do Claude Code onde estava cada mensagem; em `[replies.terms]` põe a palavra real do domínio que vais dizer em cada `<termo-N>`, de preferência uma palavra que aparece na mensagem do Claude dessa linha:

   ```
   [replies.projects]
   "<projeto-1>" = "nome-do-projeto-1"
   "<projeto-2>" = "nome-do-projeto-2"

   [replies.terms]
   "<termo-1>" = "palavra-1"
   "<termo-2>" = "palavra-2"
   ```

   Continua `[replies.terms]` com uma linha por cada `<termo-N>` do guião (são 8). Guarda o ficheiro.
5. Confirma a preparação com este comando (não liga o microfone):

   ```
   py -3.12 -m bench.record --set replies --dry-run
   ```

   Deve mostrar `replies: 10 script rows, 0 recorded, 10 pending`, `local mapping: 2 of 2 projects and 8 of 8 terms named` e `reply files: 10 of 10 saved`. Se aparecer outra coisa, para aqui e avisa o Claude.
6. Liga o headset, põe-no na cabeça, confirma que o microfone não está em silêncio (mute) e escolhe um sítio sossegado.
7. Começa a gravação com:

   ```
   py -3.12 -m bench.record --set replies
   ```

8. Para cada resposta o ecrã mostra primeiro a descrição da mensagem do Claude (não a leias em voz alta) e depois a resposta a dizer, já com a palavra real do domínio. Lê a resposta uma vez em silêncio, carrega em **Enter** para começar, espera meio segundo, diz a resposta como a dirias ao Claude Code com o rato 5 e carrega em **Enter** para parar.
9. Se aparecer «Take rejeitado», corrige o que a mensagem indica e grava a mesma resposta outra vez: o programa volta a pedi-la sozinho. Quando aparecer «Guardado», carrega em **Enter** para passar à seguinte, ou escreve `r` e Enter para a repetir. Para fazer uma pausa, escreve `q` e Enter; para continuar mais tarde, repete o passo 7.
10. No fim aparece «Gravadas 10 de 10 frases». Confirma com o comando do passo 5: deve mostrar `10 recorded, 0 pending` e `pending pairs 0`.
11. Corre a medição com este comando (usa o Whisper e o Ollama locais; nada é enviado para fora do PC). Mede os prompts, os ditados para o Claude Code e as respostas, por isso demora alguns minutos:

    ```
    .venv\Scripts\python -m bench.prompts --summary docs/research/replies-summary.json
    ```

    No fim aparece a linha `reply pairs (replies): 10 paired, 0 unpaired` e duas linhas com os números com e sem a mensagem do Claude.
12. Diz ao Claude: «gravação das respostas feita». O Claude confere os números contra as metas e escreve-os na documentação, só com contagens. Não é preciso fazer commit de nada: as mensagens, as gravações e os resultados com texto ficam nas pastas ignoradas.
