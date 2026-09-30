# Como usar o Quill

O Quill escreve o que diz em qualquer janela do Windows. Tudo corre no seu PC: o áudio nunca sai do computador.

## Preparar (uma vez)

1. Abra o Terminal do Windows na pasta do Quill.
2. Abra o ficheiro `local\quill.toml` (se não existir, crie-o) e escreva o nome do seu microfone:

   ```
   [audio]
   microphone = "Nome do microfone"
   ```

   Para ver os nomes disponíveis sem ligar o microfone, corra `py -3.12 -m bench.record --list-devices` e copie o início do nome do seu headset.
3. Confirme que está tudo pronto:

   ```
   .venv\Scripts\python -m quill --check
   ```

   Cada linha começa por `OK`, `FAIL` ou `note`. No fim deve aparecer `Quill is ready.`. Se aparecer `FAIL`, envie-me essa linha.

## Ligar e desligar

4. Ligue o Quill:

   ```
   .venv\Scripts\python -m quill
   ```

   Aparece "A carregar" junto ao ponteiro durante alguns segundos, enquanto o modelo de voz carrega. Enquanto carrega, carregar no botão não grava nada.
5. Para desligar, carregue em Ctrl+C nessa janela do Terminal.

## Ditar

6. Aponte o rato para o campo onde quer escrever.
7. Mantenha premido o botão 4 do rato (o botão lateral de trás). O Quill clica nesse campo e mostra "A ouvir".
8. Fale. As palavras aparecem ao vivo junto ao ponteiro.
9. Largue o botão. O texto limpo (sem "hum", "tipo", repetições) é escrito no campo. O Quill nunca carrega em Enter neste modo.

Também pode usar a tecla F13 (por exemplo, num botão do Streamlabs) ou o Ctrl da direita. Com o Ctrl da direita não há clique: o texto vai para onde o cursor já está.

Um toque rápido no botão não faz nada. Se carregar noutra tecla enquanto segura o Ctrl da direita (por exemplo Ctrl+C), o ditado é cancelado e o atalho funciona normalmente.

## Enviar para o Claude Code

10. No Claude Code, mantenha premido o botão 5 do rato (o botão lateral da frente), fale e largue. O indicador mostra "A rever o texto" enquanto o modelo local (Ollama) revê o texto, mesmo que seja curto. Depois o texto revisto é escrito e o Quill carrega em Enter para o enviar. Aparece "Enviado para o Claude Code". Se o Ollama falhar, demorar demais ou recusar a revisão, é enviado o texto tal como o disse, com um aviso curto. No Claude Code, o botão 5 também usa o contexto do projeto e organiza o pedido num prompt mais claro (veja "Botão 5 no Claude Code: projeto e prompt enriquecido", mais abaixo).
11. Para enviar o texto tal como o disse, sem a revisão, use o botão do meio (carregar na roda do rato) em vez do botão 5: mantenha a roda premida, fale e largue. O texto é escrito e o Quill carrega em Enter.

O Enter só é carregado no Claude Code (a caixa do Claude Code na barra lateral do VS Code, ou o Claude Code no Terminal do Windows) e só depois de o texto todo estar escrito. Em qualquer outra janela (um ficheiro ou o terminal integrado do VS Code, o browser, o WhatsApp), o botão 5 e o botão do meio escrevem o texto sem Enter e o indicador avisa "Não é o Claude Code: escrito sem Enter". O botão 4 nunca carrega em Enter.

Enquanto o Quill está ligado, o botão do meio deixa de fazer o que fazia nos outros programas (por exemplo, abrir uma ligação num separador novo ou fechar um separador no browser).

No VS Code, o Quill só reconhece o Claude Code quando ele está aberto na barra lateral e tem o foco. Prepare o VS Code uma vez:

1. No VS Code, carregue em Ctrl+Shift+P, escreva `Open User Settings (JSON)` e carregue em Enter.
2. Dentro das chavetas `{ }`, acrescente estas duas linhas (se já existir uma linha `"claudeCode.preferredLocation"`, substitua-a):

   ```
   "window.title": "${dirty}${activeEditorShort}${separator}${rootName}${separator}${profileName}${separator}${appName} [${focusedView}]",
   "claudeCode.preferredLocation": "sidebar",
   ```

3. Guarde com Ctrl+S.
4. Abra o Claude Code (o ícone do Claude na barra lateral) e clique na caixa de texto dele. O título da janela do VS Code passa a acabar em `[Claude Code]`.

A partir daí, o botão 5 na caixa do Claude Code na barra lateral envia com Enter. Num ficheiro, no terminal integrado ou num separador do Claude Code aberto no editor, o texto é escrito sem Enter.

## Reescrever texto selecionado (modo comando)

12. Selecione o texto que quer mudar (por exemplo, arraste o rato por cima de um parágrafo).
13. Mantenha premida a tecla F14, diga o que quer fazer com o texto e largue. Por exemplo: «põe isto mais formal», «traduz para inglês», «encurta», «faz uma lista». O indicador mostra "Modo comando" enquanto ouve. A tecla F14 nunca clica, por isso a seleção mantém-se.
14. O modelo local (Ollama) reescreve o texto e o Quill escreve a nova versão por cima da seleção. Se algo falhar, a seleção fica igual e o indicador diz porquê. O modo comando não funciona em terminais.

Em vez da F14 (ou além dela) pode usar a tecla F8. Para isso, em `local\quill.toml` escreva:

```
[triggers.command]
keys = ["f14", "f8"]
```

A F14 fica para um botão do Streamlabs. Enquanto o Quill está ligado, a F8 deixa de fazer o que fazia nos outros programas (no VS Code, por exemplo, deixa de saltar para o problema seguinte). Depois de mudar o ficheiro, desligue e volte a ligar o Quill.

## Comandos de voz (F9): abrir um projeto no VS Code

Com a tecla F9 dá ordens ao Quill em vez de ditar. Por agora há um comando: abrir um projeto no VS Code a partir do atalho do Windows desse projeto.

1. Mantenha premida a tecla F9. O indicador mostra "Comando de voz" e as palavras ao vivo.
2. Diga o nome do projeto. Não precisa do verbo: a F9 serve só para comandos. Pode dizer só «<nome do projeto>», ou uma frase como «abre VS Code no <nome>», «VS Code na pasta <nome>» ou «abre o projeto <nome>, se faz favor». O Quill ignora o verbo (mesmo mal ouvido), «VS Code» e as formas como o Whisper o escreve, «projeto», «pasta», as preposições e as palavras de cortesia, e procura o nome em qualquer parte da frase.
3. Largue a tecla. Se o Quill tiver a certeza do projeto, mostra "A abrir <nome>" e o VS Code abre esse projeto.
4. Se o nome não for claro, não abre nada e mostra até 3 nomes parecidos, por exemplo "Não sei qual abrir. Parecidos: alfa, alfa-public". Isto acontece quando nenhum atalho tem um nome parecido que chegue, quando disse dois nomes, ou quando dois atalhos são demasiado parecidos. Repita com o nome certo.
5. Se só disse palavras de enchimento (por exemplo «abre o VS Code»), aparece "Comando não reconhecido" e nada acontece.

A F9 nunca clica, nunca escreve texto e nunca carrega em Enter. O nome dito é só comparado com os nomes dos atalhos; nunca é executado. Um nome exato ganha a um nome mais comprido: «alfa» abre alfa e «alfa public» abre alfa-public (um nome com hífen pode ser dito como duas palavras). Na F9 o Whisper ouve sempre em português e recebe como dica os nomes dos atalhos e os nomes do seu vocabulário. Os nomes e as variantes do seu vocabulário pessoal (`local\vocabulary.toml`) também contam: se o Quill ouvir mal um nome, acrescente a forma que ele ouve como variante desse nome.

A F9 é transcrita com um modelo próprio, o large-v3 (já está no PC), que ouve melhor os nomes dos projetos; o ditado continua com o large-v3-turbo. Os dois modelos ficam carregados, o que ocupa cerca de 3 GB a mais na memória da placa gráfica. Nos primeiros segundos depois de ligar o Quill, enquanto o large-v3 carrega, a F9 usa o modelo do ditado. Para voltar a usar só um modelo, acrescente esta linha à secção `[voice_commands]` de `local\quill.toml` e volte a ligar o Quill (a F9 volta a acertar menos nomes):

```
model = "large-v3-turbo"
```

Na medição com as suas 15 gravações de comandos, com o large-v3 a F9 acertou as 15 e nunca abriu o projeto errado (meta: pelo menos 95 % e nenhum projeto errado). Com o large-v3-turbo acertava 12; nas outras 3 ouvia mal o nome e não abria nada. Detalhes em `docs/research/VOICE-COMMANDS.md`.

O Quill só abre atalhos (ficheiros `.lnk`) que estejam diretamente dentro das pastas indicadas em `local\quill.toml`, uma pasta por linha:

```
[voice_commands]
shortcut_dirs = ['D:\Projetos\Atalhos\Principais', 'D:\Projetos\Atalhos\Outros']
```

O nome do projeto é o nome do atalho sem `.lnk`. Subpastas não contam. Um atalho que abre o VS Code é aberto como num duplo clique; um atalho para uma pasta abre essa pasta no VS Code; qualquer outro atalho é recusado. O seu `local\quill.toml` já tem as pastas de atalhos dos seus projetos (sem as pastas de cópias de segurança). Depois de mudar as pastas, confirme e volte a ligar o Quill:

```
py -3.12 -m quill.config --check local/quill.toml
```

Enquanto o Quill está ligado, a F9 deixa de chegar aos outros programas (no VS Code, por exemplo, deixa de pôr ou tirar um ponto de paragem). Se precisar da F9, escolha outra tecla em `local\quill.toml`, por exemplo:

```
[triggers.voice]
keys = ["f18"]
```

Só pode haver uma tecla, e nunca um botão do rato (o botão 4 fica para ditar). Com `keys = []` os comandos de voz ficam desligados.

## Corrigir erros

15. Se uma palavra sair mal, corrija-a à mão logo a seguir, ou selecione o texto já corrigido e carregue em F16. O Quill aprende a troca. Quando a mesma troca acontecer em dois ditados, passa a ser feita sozinha.
16. Uma vez por semana, reveja o que o Quill aprendeu:

    ```
    py -3.12 -m quill.review
    ```

    Para cada correção, escolha se a aprova ou apaga.

## Ditados longos: revisão automática e tecla de desfazer

Quando fala mais de 15 segundos (ou diz mais de 40 palavras), o Quill pede ao modelo local (Ollama) que reveja o texto antes de o escrever. Ele corrige palavras mal ouvidas a partir do contexto (o tipo de janela, o projeto aberto no VS Code e o seu vocabulário) e não tira nenhuma informação. Demora mais 1 a 3 segundos. Os ditados curtos são escritos como sempre, sem esta revisão. O botão 5 revê sempre, seja qual for o tamanho, e o botão do meio nunca revê.

17. Dite um texto longo como de costume. Depois de largar, o indicador mostra "A rever o texto" e a seguir o texto revisto é escrito.
18. No Claude Code na barra lateral do VS Code, um pedido com vários passos pode ficar em várias linhas (entre linhas o Quill usa Shift+Enter, nunca Enter). Com o botão 5, o Enter que envia só é carregado depois de o texto todo estar escrito. No terminal, o texto fica num só parágrafo.
19. Se preferir o texto tal como o disse, carregue em F17 logo a seguir. O Quill apaga o texto revisto e escreve o original no mesmo sítio. Só funciona nos 30 segundos seguintes, na mesma janela, antes de carregar em Enter (por isso não serve depois de enviar com o botão 5 no Claude Code) e se não tiver mexido no texto (o cursor tem de estar no fim dele). Caso contrário, nada muda e o indicador diz porquê, por exemplo "O texto foi editado; a reescrita ficou".

A F17 não existe na maioria dos teclados: atribua-a a um botão do Streamlabs, como a F13 e a F14. Para usar outra tecla, escreva em `local\quill.toml` (não pode ser uma tecla de ditado, envio ou comando, nem a F16 da correção):

```
[autorewrite]
undo_key = "f18"
```

Se o Ollama estiver desligado, demorar mais de 4 segundos ou a revisão perder alguma informação, o Quill escreve logo o texto original e o indicador avisa. Para desligar a revisão automática, escreva em `local\quill.toml`:

```
[autorewrite]
enabled = false
```

Depois de mudar o ficheiro, desligue e volte a ligar o Quill.

## Botão 5 no Claude Code: projeto e prompt enriquecido

Quando dita com o botão 5 para o Claude Code, o Quill sabe em que projeto está e usa isso para corrigir palavras mal ouvidas. Por exemplo, num projeto de trading, se o Whisper ouvir mal «wallet», o modelo local pode trocar a palavra pelo termo certo do projeto, desde que soe parecido e faça sentido na frase. Depois transforma o que disse num prompt organizado para o Claude Code. Isto demora mais alguns segundos do que a revisão normal.

- **Como descobre o projeto:** no VS Code, pelo título da janela (o formato da Central de Projetos «<projeto> | <ficheiro> - Visual Studio Code [<vista>]» ou o título normal com a pasta). No Claude Code num terminal, por um nome de projeto conhecido no título ou pela pasta onde essa sessão do Claude Code está a trabalhar. O nome leva à pasta do projeto pelos atalhos dos comandos de voz (a pasta ou o `.code-workspace` para onde apontam) ou pela lista `[project_context.folders]` em `local\quill.toml`, por exemplo:

  ```
  [project_context.folders]
  "projeto-exemplo" = 'D:\Projetos\projeto-exemplo'
  ```

  Se não reconhecer o projeto, o botão 5 funciona como antes da mudança, mas o prompt continua a ser organizado.
- **O que lê do projeto:** um resumo curto do que o projeto é (do `CLAUDE.md`, ou do `README.md` se não houver) e uma lista de termos do próprio projeto (títulos da documentação, nomes de módulos, ficheiros e classes, palavras que o projeto repete). Só lê ficheiros que o Git acompanha ou acompanharia, por isso nada do que o `.gitignore` do projeto exclui. Nunca lê ficheiros `.env`, chaves, certificados, credenciais, ficheiros com «token» ou «secret» no nome, áudio, vídeo, gravações nem a pasta `local`. A pasta tem de ser um repositório Git; se não for, não há contexto. O resumo fica guardado em `local\context` (nunca vai para o Git) e é refeito quando o projeto muda ou ao fim de 24 horas. Nada sai do PC e o registo nunca guarda o conteúdo do projeto.
- **O prompt enriquecido:** o texto corrigido é organizado em partes com títulos, na língua em que falou: «Objetivo», «Contexto», «Pedido», «Restrições» e «Critérios de aceitação» (em inglês, «Objective», «Context», «Request», «Constraints», «Acceptance criteria»). Só entram as partes que disse. O Quill confere a resposta do modelo: não pode perder nenhuma palavra ou número que disse, nem acrescentar números, requisitos ou palavras que não disse. Do projeto só pode acrescentar contexto, e só na parte «Contexto». Se a resposta falhar essa verificação, é enviado o texto corrigido.
- **Como é escrito:** na caixa do Claude Code da barra lateral do VS Code, cada parte fica na sua linha (Shift+Enter entre linhas). No Claude Code num terminal, fica tudo num só parágrafo, com os títulos. No fim, o Quill carrega uma só vez em Enter.
- **O que o indicador mostra:** "A enriquecer o prompt para o Claude Code…" enquanto o modelo organiza o texto e, depois do Enter, "Prompt enriquecido e enviado". Se o prompt não puder ser enriquecido, aparece "Enriquecimento recusado; foi o texto corrigido", "Ollama indisponível; foi o texto corrigido" ou "O enriquecimento demorou demais; foi o texto corrigido", e foi enviado o texto corrigido.
- **Ditados curtos:** com menos de 6 palavras (por exemplo «sim, continua») o texto é só corrigido, sem ser organizado.
- **Desfazer:** a F17 continua a repor o que disse, antes da correção e da organização, sempre que não houve Enter. Como o botão 5 no Claude Code envia com Enter, aí não há nada para desfazer.
- O botão 4, o botão do meio e as outras janelas continuam como antes: sem contexto do projeto e sem prompt enriquecido.

Para mudar o tempo máximo da organização (em segundos, de 1 a 60; por omissão 15), escreva em `local\quill.toml`:

```
[autorewrite]
enrich_timeout_s = 20
```

Para ver quantos termos o Quill encontra em cada projeto da lista (só contagens, nunca o conteúdo):

    py -3.12 -m quill.context_pack --check

Depois de mudar o ficheiro, desligue e volte a ligar o Quill.

## Acrescentar palavras ao vocabulário

O vocabulário pessoal fica em `local\vocabulary.toml`, que nunca vai para o Git. Serve para nomes de projeto e termos técnicos que o Quill ouve mal (por exemplo, termos de trading).

20. Abra o Terminal do Windows na pasta do Quill e acrescente o termo, escrito como quer que apareça no texto (com as maiúsculas certas):

    ```
    py -3.12 -m quill.vocabulary --add "take profit"
    ```

    Pode acrescentar vários de uma vez: `--add "Bybit" --add "funding rate"`.
21. Para um nome de projeto, use `--add-name` em vez de `--add`:

    ```
    py -3.12 -m quill.vocabulary --add-name "nome-do-projeto"
    ```

22. Se o Quill escrever sempre a mesma coisa errada no lugar de um termo, ensine essa forma: primeiro o termo certo, depois o que o Quill escreve.

    ```
    py -3.12 -m quill.vocabulary --add-variant "take profit" "teique profit"
    ```

23. Leia a resposta. Mostra só contagens: quantos entraram (`added`), quantos já lá estavam (`already listed`) e quantas dicas cabem no Whisper (`hints ... fit the prompt, ... dropped`). Se aparecer um erro, o ficheiro ficou igual e a mensagem diz qual o campo com problema. Um termo repetido (mesmo com outras maiúsculas ou acentos) não é acrescentado de novo.
24. Não é preciso desligar o Quill: o vocabulário novo é usado a partir do ditado seguinte. Se o ficheiro ficar inválido (por exemplo, depois de o editar à mão), o Quill continua com o vocabulário anterior e escreve o aviso em `local\logs\quill.log`. Para confirmar que o ficheiro está bem: `py -3.12 -m quill.vocabulary --check`.

O Whisper recebe as dicas por esta ordem: nomes de projeto, a lista de termos técnicos genéricos do Quill e depois os seus termos. O espaço é pequeno e hoje já está cheio com os nomes e a lista genérica, por isso os seus termos aparecem como `dropped`. Mesmo assim contam: depois do reconhecimento, uma palavra muito parecida com um termo passa a ter a grafia certa. Se o Quill escrever um termo de forma muito diferente, use o passo 22 com essa forma.

## Aviso quando o Claude Code termina

Quando o Claude Code (no painel do VS Code ou no terminal) acaba a resposta e fica à sua espera, o Quill toca um som curto e o indicador mostra "Claude Code terminou". Quando o Claude Code pede uma permissão, toca outro som e o indicador mostra "Claude Code pede permissão". Não avisa a meio da resposta (quando o Claude Code usa ferramentas ou escreve texto intermédio). Se estiver a ditar, o aviso espera que o ditado acabe, para o som nunca entrar no microfone; vários avisos seguidos tocam uma só vez. As execuções automáticas do Claude Code (`claude -p`, o Agent SDK e as execuções automáticas neste PC) não tocam. O aviso não usa a rede: o Claude Code avisa o Quill dentro do próprio PC.

O aviso diz de que projeto é. O indicador mostra o nome da pasta do projeto (a pasta do repositório Git onde o Claude Code está a trabalhar), por exemplo "projeto-exemplo: Claude acabou" ou "projeto-exemplo: Claude pede permissão". Se vários projetos estiverem à espera, aparecem todos, com os pedidos de permissão no fim. Logo a seguir ao som, uma voz do Windows diz o nome do projeto (no máximo três nomes, primeiro os que pedem permissão; os hífenes, sublinhados e pontos são lidos como espaços). Se começar a ditar enquanto a voz fala, ela cala-se logo, antes de o microfone abrir. Um aviso que chega poucos segundos depois de outro não toca nem fala: só aparece no indicador. Quando o Quill não recebe o nome, o aviso aparece e toca como antes, sem nome.

Instalar (uma vez):

25. Abra o Terminal do Windows na pasta do Quill e veja a alteração que vai ser feita nas definições do Claude Code. Este comando não escreve nada:

    ```
    py -3.12 -m quill.claude_hooks --show
    ```

26. Instale:

    ```
    py -3.12 -m quill.claude_hooks --install
    ```

    Mostra a mesma alteração e pergunta se a quer escrever. Escreva `yes` e carregue em Enter. As suas outras definições e hooks do Claude Code ficam iguais, e o ficheiro anterior fica guardado ao lado (`settings.json.bak-quill-...`). Se o aviso já estiver instalado, nada muda. Se mudar a pasta do Quill de sítio, corra de novo este comando.
27. Feche e volte a abrir o Claude Code (a janela do VS Code e o terminal), para ele ler a alteração. Se o Quill já estava ligado antes desta versão, desligue-o e volte a ligá-lo.

Se o aviso já estava instalado antes de o Quill dizer o nome do projeto, não precisa de o instalar outra vez: basta desligar o Quill e voltar a ligá-lo.

Testar:

28. Para ouvir os dois sons, um de cada vez:

    ```
    py -3.12 -m quill.sound --play-sound
    ```

    Para ouvir um exemplo com o nome do projeto (o primeiro som e depois a voz a dizer "projeto exemplo"):

    ```
    py -3.12 -m quill.sound --play-sound --speak
    ```

    Para ouvir outro nome, acrescente `--project` e o nome, por exemplo `--project meu-projeto`.

29. Com o Quill ligado, no painel do Claude Code do VS Code, peça uma coisa curta (por exemplo "diz olá"). Quando a resposta acabar, ouve o primeiro som e a voz a dizer o nome do projeto, e o indicador mostra "Claude Code terminou" com "<projeto>: Claude acabou" durante uns segundos.
30. Repita no Claude Code do terminal.
31. Peça uma coisa que precise de autorização (por exemplo, correr um comando que ainda não autorizou). Ouve o segundo som e o nome do projeto, e o indicador mostra "Claude Code pede permissão" com "<projeto>: Claude pede permissão".
32. Peça uma resposta mais longa e, enquanto ela é escrita, segure o botão de ditado e fale. O som só toca depois de largar o botão e o texto ser escrito.

Se não ouvir nada: confirme que o Quill está ligado, que os sons do Windows estão ligados (Definições > Sistema > Som > Mais definições de som > separador Sons: "Asterisco" e "Exclamação") e, no Claude Code, escreva `/hooks` para ver as duas entradas do Quill (Stop e Notification).

Para deixar só o indicador, sem som, ou desligar o aviso, escreva em `local\quill.toml` e depois desligue e volte a ligar o Quill:

```
[claude_alert]
sound = false
enabled = true
```

Com `sound = false` também não se ouve o nome. Para ouvir só o som, sem a voz a dizer o nome (o indicador continua a mostrar o projeto):

```
[claude_alert]
speak_project = false
```

Para mudar o volume da voz (de 0 a 100; hoje 80) ou a velocidade (1.0 é a velocidade normal; 1.2 é um pouco mais rápido, 0.8 um pouco mais lento):

```
[claude_alert]
speech_volume = 60
speech_rate = 1.2
```

A voz é uma das que o Windows já tem; o Quill não instala nada e o nome não sai do PC. Se o Windows tiver uma voz de português de Portugal, é essa que fala. Se não tiver, fala outra voz portuguesa e, se também não houver, a voz normal do Windows (por exemplo em inglês). Se o Windows não tiver voz nenhuma, toca só o som e o indicador continua a mostrar o nome. Para saber que voz vai falar, sem tocar nada:

```
py -3.12 -m quill.speech --probe
```

Mostra só a língua da voz (por exemplo `pt-PT`) e o tamanho do áudio de teste.

Limites do filtro das execuções automáticas: o Quill usa uma indicação que o Claude Code dá a cada hook (`CLAUDE_CODE_SESSION_ATTENDED`: `1` quando alguém está a usar a sessão, `0` quando é automática). Foi confirmada na versão do Claude Code instalada neste PC. Uma versão que não dê esta indicação nunca toca; nesse caso use `filter = "unless-headless"` em `[claude_alert]` (toca sempre, menos quando a indicação diz que a sessão é automática). Um `claude -p` que corra à mão também não toca, porque também é automático. Com `filter = "all"` toca em todas as sessões, também nas automáticas.

Remover:

33. Na pasta do Quill:

    ```
    py -3.12 -m quill.claude_hooks --remove
    ```

    Mostra o que sai, escreva `yes` e carregue em Enter. Só saem as entradas do Quill; as outras definições ficam. Feche e volte a abrir o Claude Code.

## Arrancar com o Windows

34. Para o Quill arrancar sozinho quando entra no Windows, corra uma vez:

    ```
    .venv\Scripts\python -m quill --install-startup
    ```

    Para deixar de arrancar com o Windows: `.venv\Scripts\python -m quill --remove-startup`. Se mudar a pasta do Quill de sítio, corra de novo o `--install-startup`.
35. Quando arranca com o Windows não há janela do Terminal. Para o desligar: `.venv\Scripts\python -m quill --stop`.

## Se algo correr mal

O indicador mostra "Erro" com a causa e o ditado seguinte funciona normalmente:

- "Microfone indisponível; verifique o headset": ligue o headset e tente de novo.
- "Nenhum campo de texto sob o ponteiro": aponte para um campo de texto antes de premir.
- "A janela ativa mudou; o texto não foi escrito": não mude de janela até o texto aparecer.
- "Janela de administrador: não é possível escrever": o Quill não escreve em janelas abertas como administrador.
- "Ollama indisponível: texto limpo pelas regras": o texto foi escrito na mesma, limpo pelas regras.
- "Ollama indisponível; ficou o texto original", "A reescrita demorou demais; ficou o texto original" ou "Reescrita recusada; ficou o texto original": o ditado foi escrito sem a revisão automática, tal como o disse.
- "Enriquecimento recusado; foi o texto corrigido", "Ollama indisponível; foi o texto corrigido" ou "O enriquecimento demorou demais; foi o texto corrigido": com o botão 5 no Claude Code, foi enviado o texto corrigido, sem ser organizado em prompt.
- "Não há reescrita para desfazer", "Outra janela ativa; a reescrita ficou", "Já carregou em Enter; a reescrita ficou" e parecidos: a F17 não mudou nada, pelo motivo indicado.
- "A reposição foi interrompida; verifique o texto": a F17 parou a meio (por exemplo, mudou de janela); confirme o texto no campo.
- "Selecione o texto antes de dar a instrução": o modo comando precisa de texto selecionado.
- "Ollama indisponível; a seleção ficou igual": abra o Ollama e repita a instrução.

O registo fica em `local\logs\quill.log`. Tem só eventos e tempos, nunca o que disse ou escreveu.
