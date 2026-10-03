# Gravar os nomes dos projetos na tua voz (Fase 10)

O objetivo é gravar a tua voz a dizer, uma vez, o nome de cada projeto que o aviso do Claude Code pode dizer. Depois disso, quando o Claude Code acaba ou pede permissão, o Quill toca o som do aviso e a seguir a tua gravação do nome, em vez da voz sintética do Windows. Um nome que não gravaste continua a ser dito pela voz do Windows, como hoje.

As gravações ficam só neste PC, na pasta `local\names` do projeto, que o Git ignora (cada nome num ficheiro WAV e uma lista `manifest.json` que liga o nome ao ficheiro). Nada é enviado para a internet nem publicado. O microfone só abre enquanto corre o comando de gravação do passo 5.

Tempo previsto: 10 a 15 minutos (cada nome demora uns segundos).

## Passos

1. Liga o headset Razer BlackShark V2 Pro (dongle USB ligado), põe-no na cabeça e confirma que o microfone não está em silêncio (mute). Escolhe um sítio sossegado.
2. Abre o VS Code na pasta do projeto `quill` e abre um terminal: menu **Terminal → New Terminal**.
3. Escreve este comando e carrega em Enter:

   ```
   py -3.12 -m quill.names --dry-run
   ```

   Este comando não liga o microfone. Mostra duas linhas:
   - `name sources: claude_code ..., folders ..., vocabulary ..., added 0`: quantos nomes vieram de cada sítio (os projetos que o Claude Code já abriu, as pastas de projetos e atalhos do Quill e o vocabulário). Os números não podem ser todos 0.
   - `names found: N, recorded: 0, missing: N, skipped: 0`: N é o número de nomes a gravar (os nomes repetidos contam uma vez).

   Se aparecer `error:` ou `names found: 0`, para aqui e avisa o Claude.
4. Confirma o microfone com:

   ```
   py -3.12 -m quill.names --list-devices
   ```

   Deve aparecer uma linha com `Razer BlackShark V2` e, no fim dela, `<- configured`. Se essa marca não aparecer, para aqui e avisa o Claude.
5. Começa a gravação com:

   ```
   py -3.12 -m quill.names
   ```

6. Para cada nome o ecrã mostra o número (por exemplo `[3/20]`) e `Nome:` com o nome. Quando o nome tem hífenes, sublinhados ou pontos, aparece também `Diz:` com a forma a dizer: num nome como `x-public`, diz as duas partes seguidas («x public»), como o queres ouvir no aviso.
   - Diz só o nome, uma vez, ao teu ritmo normal e no tom em que o queres ouvir. Não digas mais nada antes nem depois.
7. Carrega em **Enter** para começar, espera meio segundo, diz o nome e carrega em **Enter** para parar. O programa corta o silêncio do princípio e do fim e acerta o volume, para todos os nomes soarem igual.
8. Se aparecer «Take rejeitado», a gravação tinha um problema (microfone em silêncio, som demasiado baixo, gravação demasiado curta, nome com mais de 4 segundos ou falha do áudio). Corrige o que a mensagem indica e grava o mesmo nome outra vez: o programa volta a pedi-lo sozinho.
9. Quando aparecer «Guardado», carrega em **Enter** para passar ao nome seguinte. Se te enganaste (outra palavra, um corte, um ruído forte), escreve `r` e Enter para gravar esse nome outra vez; a gravação nova substitui a anterior.
10. Um nome que não queres gravar (por exemplo uma pasta que já não usas): antes de gravar, escreve `s` e Enter. O programa salta-o e não o volta a pedir; esse nome continua a ser dito pela voz do Windows. Para voltar a ver os nomes saltados:

    ```
    py -3.12 -m quill.names --include-skipped
    ```

11. Se faltar um nome na lista, acrescenta-o e grava-o logo (troca `<nome>` pelo nome da pasta do projeto, entre aspas se tiver espaços):

    ```
    py -3.12 -m quill.names --add <nome>
    ```

    Para regravar mais tarde um nome que já gravaste:

    ```
    py -3.12 -m quill.names --redo <nome>
    ```

12. Para fazer uma pausa, escreve `q` e Enter. Para continuar mais tarde, repete o passo 5: o programa recomeça no primeiro nome que ainda não foi gravado nem saltado.
13. No fim, confirma com:

    ```
    py -3.12 -m quill.names --dry-run
    ```

    Deve mostrar `missing: 0` (os nomes que saltaste aparecem em `skipped`).
14. Desliga o Quill e volta a ligá-lo.
15. Para ouvir um exemplo (o som do aviso e depois a tua gravação), troca `<nome>` por um nome que gravaste:

    ```
    py -3.12 -m quill.sound --play-sound --speak --project <nome>
    ```

    A primeira linha diz `own-voice clip` quando toca a tua gravação, ou `Windows voice` quando esse nome não tem gravação. O volume é o de `speech_volume` em `[claude_alert]` (de 0 a 100).
16. Diz ao Claude: «gravação dos nomes feita». Não é preciso fazer commit de nada.

## Desligar

Para voltar à voz do Windows em todos os nomes, sem apagar as gravações, escreve em `local\quill.toml` e depois desliga e volta a ligar o Quill:

```
[claude_alert]
own_voice = false
```

Para voltar a usar as gravações, muda para `own_voice = true` (ou apaga a linha) e reinicia o Quill.
