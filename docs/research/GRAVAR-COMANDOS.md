# Gravar o guião de comandos de voz (Fase 4)

O objetivo é gravar a tua voz a dizer 15 comandos curtos, como os que vais dizer com a tecla F9 («abre VS Code no <projeto>»). Estas gravações servem para medir, com a tua voz real, se o Quill abre o projeto certo: a meta é pelo menos 95 % de ações certas (com 15 comandos, são as 15) e nenhum atalho errado aberto.

Durante a gravação nada é aberto: o programa só grava. As gravações ficam só neste PC, na pasta `local/` do projeto, que o Git ignora. Nada é enviado para a internet nem publicado.

Tempo previsto: 5 a 10 minutos.

## Passos

1. Liga o headset Razer BlackShark V2 Pro (dongle USB ligado), põe-no na cabeça e confirma que o microfone não está em silêncio (mute). Escolhe um sítio sossegado.
2. Abre o VS Code na pasta do projeto `quill` e abre um terminal: menu **Terminal → New Terminal**.
3. Escreve este comando e carrega em Enter:

   ```
   py -3.12 -m bench.voice_commands --dry-run
   ```

   Deve mostrar `recorded 0 of 15`, `names: 13 of 13` e `negatives without a shortcut 2 of 2`. Este comando não liga o microfone. Se aparecer outra coisa, para aqui e avisa o Claude.
4. Confirma o microfone com:

   ```
   py -3.12 -m bench.record --list-devices
   ```

   Deve aparecer uma linha com `Razer BlackShark V2` e, no fim dela, `<- configured`. Se essa marca não aparecer, para aqui e avisa o Claude.
5. Começa a gravação com:

   ```
   py -3.12 -m bench.record --set voice
   ```

6. Para cada comando o ecrã mostra o número, o caso (`exato`, `irmão`, `vocabulário`, `variante` ou `negativo`) e a frase, já com o nome real do projeto. Lê a frase uma vez em silêncio antes de gravar.
   - Diz a frase como a dirias com a tecla F9, ao teu ritmo normal, do princípio ao fim (incluindo «podes», «abre-me» ou «por favor» quando aparecem).
   - Diz os nomes dos projetos como costumas dizer. Num nome com hífen, como `x-public`, diz as duas partes seguidas («x public»).
   - Os dois comandos `negativo` têm nomes inventados que não são projetos teus: diz-os na mesma, tal como estão escritos.
7. Carrega em **Enter** para começar, espera meio segundo, diz o comando e carrega em **Enter** para parar.
8. Se aparecer «Take rejeitado», a gravação tinha um problema (microfone em silêncio, som demasiado baixo, gravação demasiado curta ou falha do áudio). Corrige o que a mensagem indica e grava o mesmo comando outra vez: o programa volta a pedi-lo sozinho. Os comandos são curtos: fala logo depois de carregar em Enter e para só depois de acabares a frase.
9. Quando aparecer «Guardado», carrega em **Enter** para passar ao comando seguinte. Se te enganaste em algo que não está no texto (outra palavra, um corte, um ruído forte), escreve `r` e Enter para gravar esse comando outra vez; a gravação anterior fica de lado e não é usada.
10. Para fazer uma pausa, escreve `q` e Enter. Para continuar mais tarde, repete o passo 5: o programa recomeça no primeiro comando que ainda não foi gravado.
11. No fim aparece «Gravadas 15 de 15 frases». Confirma com:

    ```
    py -3.12 -m bench.voice_commands --dry-run
    ```

    Deve mostrar `recorded 15 of 15` e `complete: yes`.
12. Diz ao Claude: «gravação dos comandos de voz feita». O Claude corre a medição (nada é aberto) e mostra-te os números contra a meta. Não é preciso fazer commit de nada.
