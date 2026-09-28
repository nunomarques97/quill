# Gravar o guião de ditado (Fase 2)

O objetivo é gravar a tua voz a ditar 36 frases mais longas (5 a 30 segundos), como se estivesses a escrever um prompt no Claude Code, código no VS Code, uma mensagem no WhatsApp ou um email. Estas gravações servem para medir o Quill com a tua voz real. Contam no mínimo 30 frases válidas; o ideal são as 36.

As gravações ficam só neste PC, na pasta `local/` do projeto, que o Git ignora. Nada é enviado para a internet nem publicado.

Tempo previsto: 20 a 30 minutos. Podes parar a meio e continuar noutro dia.

## Passos

1. Liga o headset Razer BlackShark V2 Pro (dongle USB ligado), põe-no na cabeça e confirma que o microfone não está em silêncio (mute). Escolhe um sítio sossegado.
2. Abre o VS Code na pasta do projeto `quill` e abre um terminal: menu **Terminal → New Terminal**.
3. Escreve este comando e carrega em Enter:

   ```
   py -3.12 -m bench.record --list-devices
   ```

   Deve aparecer uma linha com `Razer BlackShark V2` e, no fim dela, `<- configured`. Este comando não liga o microfone. Se essa marca não aparecer, para aqui e avisa o Claude.
4. Começa a gravação com:

   ```
   py -3.12 -m bench.record
   ```

5. Para cada frase o ecrã mostra o número, o estilo (`claude-code`, `vscode`, `whatsapp` ou `email`) e o texto. Lê a frase uma vez em silêncio antes de gravar.
   - As palavras seguidas de `…` fazem parte do ditado natural: diz as hesitações (`hum…`, `pronto…`, `tipo…`, `é pá…`) e repete as palavras marcadas, como está escrito. Por exemplo, `corre os… corre os testes` diz-se «corre os, corre os testes».
   - Os nomes dos projetos já aparecem escritos; diz-os como costumas dizer.
   - Fala ao teu ritmo normal, como se estivesses a ditar a sério para esse tipo de janela.
6. Carrega em **Enter** para começar, espera meio segundo, diz a frase e carrega em **Enter** para parar.
7. Se aparecer «Take rejeitado», a gravação tinha um problema (microfone em silêncio, som demasiado baixo, gravação demasiado curta ou falha do áudio). Corrige o que a mensagem indica e grava a mesma frase outra vez: o programa volta a pedi-la sozinho.
8. Quando aparecer «Guardado», carrega em **Enter** para passar à frase seguinte. Se te enganaste em algo que não está no texto (outra palavra, um corte, um ruído forte), escreve `r` e Enter para gravar essa frase outra vez; a gravação anterior fica de lado e não é usada.
9. Para fazer uma pausa, escreve `q` e Enter. Para continuar mais tarde, repete o passo 4: o programa recomeça na primeira frase que ainda não foi gravada.
10. No fim aparece «Gravadas 36 de 36 frases». Confirma com:

    ```
    py -3.12 -m bench.pipeline --set dictation --dry-run
    ```

    Deve mostrar `recorded 36` e `complete: yes`.
11. Diz ao Claude: «gravação do ditado feita». Não é preciso fazer commit de nada.
