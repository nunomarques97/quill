# Gravar o guião de prompts para o Claude Code (Fase 6)

O objetivo é gravar a tua voz a dizer 15 pedidos curtos ao Claude Code, como os que envias com o botão 5 do rato, cada um sobre um dos teus projetos e com uma palavra do domínio desse projeto (o tipo de palavra que o Whisper às vezes ouve mal). Estas gravações servem para medir, com a tua voz real, se o rato 5 com o contexto do projeto corrige essas palavras: a meta é ter no máximo metade dos erros de hoje nessas palavras, sem nenhuma palavra perdida e sem nada inventado.

Durante a gravação nada é enviado para o Claude Code: o programa só grava. As gravações ficam só neste PC, na pasta `local/` do projeto, que o Git ignora. Nada é enviado para a internet nem publicado.

Os nomes dos projetos e as palavras do domínio que aparecem no ecrã vêm da configuração local `local/bench.toml` (secções `[prompts.projects]` e `[prompts.terms]`), que o Git ignora; o guião publicado só tem `<projeto-N>` e `<termo-N>`.

Tempo previsto: 10 a 15 minutos.

## Passos

1. Liga o headset Razer BlackShark V2 Pro (dongle USB ligado), põe-no na cabeça e confirma que o microfone não está em silêncio (mute). Escolhe um sítio sossegado.
2. Abre o VS Code na pasta do projeto `quill` e abre um terminal: menu **Terminal → New Terminal**.
3. Escreve este comando e carrega em Enter:

   ```
   py -3.12 -m bench.prompts --dry-run
   ```

   Deve mostrar `recorded 0 of 15`, `local mapping: 4 of 4 projects and 15 of 15 terms named` e `terms in their project's pack 15 of 15`. Este comando não liga o microfone nem o modelo. Se aparecer outra coisa (por exemplo `fixture problems`), para aqui e avisa o Claude.
4. Confirma o microfone com:

   ```
   py -3.12 -m bench.record --list-devices
   ```

   Deve aparecer uma linha com `Razer BlackShark V2` e, no fim dela, `<- configured`. Se essa marca não aparecer, para aqui e avisa o Claude.
5. Começa a gravação com:

   ```
   py -3.12 -m bench.record --set prompts
   ```

6. Para cada pedido o ecrã mostra o número, o caso (`termo`, `restrição` ou `números`) e a frase, já com o nome real do projeto e a palavra real do domínio. Lê a frase uma vez em silêncio antes de gravar.
   - Diz a frase como a dirias ao Claude Code com o rato 5, ao teu ritmo normal, do princípio ao fim.
   - Diz o nome do projeto e a palavra do domínio como costumas dizer no dia a dia (em inglês, se é assim que a dizes). Num nome com hífen, diz as partes seguidas.
   - Diz os números como números («trinta dias», «dois anos»).
   - Se uma palavra do domínio não é uma que uses de facto nesse projeto, escreve `q` e Enter e avisa o Claude: ele troca-a em `[prompts.terms]` antes de continuares.
7. Carrega em **Enter** para começar, espera meio segundo, diz o pedido e carrega em **Enter** para parar.
8. Se aparecer «Take rejeitado», a gravação tinha um problema (microfone em silêncio, som demasiado baixo, gravação demasiado curta ou falha do áudio). Corrige o que a mensagem indica e grava o mesmo pedido outra vez: o programa volta a pedi-lo sozinho.
9. Quando aparecer «Guardado», carrega em **Enter** para passar ao pedido seguinte. Se te enganaste em algo que não está no texto (outra palavra, um corte, um ruído forte), escreve `r` e Enter para gravar esse pedido outra vez; a gravação anterior fica de lado e não é usada.
10. Para fazer uma pausa, escreve `q` e Enter. Para continuar mais tarde, repete o passo 5: o programa recomeça no primeiro pedido que ainda não foi gravado.
11. No fim aparece «Gravadas 15 de 15 frases». Confirma com:

    ```
    py -3.12 -m bench.prompts --dry-run
    ```

    Deve mostrar `recorded 15 of 15` e `complete: yes`.
12. Diz ao Claude: «gravação dos prompts feita». O Claude corre a medição (nada é enviado para o Claude Code) e mostra-te os números contra a meta. Não é preciso fazer commit de nada.
13. A medição deixa também exemplos antes e depois do enriquecimento num ficheiro `exemplos.md`, numa pasta dentro de `bench/results/prompts/` (ignorada pelo Git; o Claude diz-te qual). Abre-o e, por baixo de cada pedido, a seguir a `Sponsor:`, escreve `sim` se o prompt enriquecido é melhor e fiel ao que disseste, ou `não` com o motivo. Diz ao Claude quando acabares.
