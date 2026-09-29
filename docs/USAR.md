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

10. No Claude Code, mantenha premido o botão 5 do rato (o botão lateral da frente), fale e largue. O texto é escrito e o Quill carrega em Enter para o enviar. Aparece "Enviado para o Claude Code".
11. Se usar o botão 5 noutra janela, o texto é escrito sem Enter e o indicador avisa "Não é o Claude Code: escrito sem Enter".

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

## Corrigir erros

15. Se uma palavra sair mal, corrija-a à mão logo a seguir, ou selecione o texto já corrigido e carregue em F16. O Quill aprende a troca. Quando a mesma troca acontecer em dois ditados, passa a ser feita sozinha.
16. Uma vez por semana, reveja o que o Quill aprendeu:

    ```
    py -3.12 -m quill.review
    ```

    Para cada correção, escolha se a aprova ou apaga.

## Arrancar com o Windows

17. Para o Quill arrancar sozinho quando entra no Windows, corra uma vez:

    ```
    .venv\Scripts\python -m quill --install-startup
    ```

    Para deixar de arrancar com o Windows: `.venv\Scripts\python -m quill --remove-startup`. Se mudar a pasta do Quill de sítio, corra de novo o `--install-startup`.
18. Quando arranca com o Windows não há janela do Terminal. Para o desligar: `.venv\Scripts\python -m quill --stop`.

## Se algo correr mal

O indicador mostra "Erro" com a causa e o ditado seguinte funciona normalmente:

- "Microfone indisponível; verifique o headset": ligue o headset e tente de novo.
- "Nenhum campo de texto sob o ponteiro": aponte para um campo de texto antes de premir.
- "A janela ativa mudou; o texto não foi escrito": não mude de janela até o texto aparecer.
- "Janela de administrador: não é possível escrever": o Quill não escreve em janelas abertas como administrador.
- "Ollama indisponível: texto limpo pelas regras": o texto foi escrito na mesma, limpo pelas regras.
- "Selecione o texto antes de dar a instrução": o modo comando precisa de texto selecionado.
- "Ollama indisponível; a seleção ficou igual": abra o Ollama e repita a instrução.

O registo fica em `local\logs\quill.log`. Tem só eventos e tempos, nunca o que disse ou escreveu.
