# Guião de comandos de voz — português europeu

Comandos que o Sponsor diz ao microfone com
`py -3.12 -m bench.record --set voice` para medir os comandos de voz do
Quill (gatilho F9): segura-se a tecla, diz-se o comando e larga-se. Só a voz
real dele conta; áudio sintético nunca entra nas medições.

Ficheiro versionado e público: nenhum nome nem caminho real entra aqui. Os
projetos aparecem só como `<projeto-N>`; o gravador mostra no ecrã, no lugar
deles, os nomes dos atalhos da central de projetos definidos em
`[voice.projects]` de `local/bench.toml` (ignorado pelo Git), e é isso que se
diz em voz alta. Os dois negativos usam nomes inventados que não têm atalho.
As gravações ficam em `local/recordings/voice/` (ignorada).

Colunas (o carregador em `bench/voice_commands.py` valida-as):

- `caso`: o que o comando põe à prova:
  - `exato`: o nome de um atalho, dito como está escrito;
  - `irmão`: um de dois atalhos com nomes parecidos (`x` e `x-public`); tem
    de abrir exatamente o dito, nunca o irmão;
  - `vocabulário`: um nome do vocabulário pessoal, que a transcrição tende a
    escrever de outra forma;
  - `variante`: outra forma de dizer «VS Code» ou de pedir
    («Visual Studio Code», «Visual Studio», «podes abrir», «abre-me»);
  - `negativo`: um projeto sem atalho; não se abre nada;
- `frase`: o comando falado;
- `intenção`: a ação esperada, `abrir` (abre o atalho da coluna `projeto`)
  ou `nada` (não abre nenhum atalho);
- `projeto`: o atalho que tem de abrir, `<projeto-N>`, ou `—` quando não se
  abre nada.

| id | caso | frase | intenção | projeto |
|----|------|-------|----------|---------|
| vc-01 | exato | Abre VS Code no <projeto-1>. | abrir | <projeto-1> |
| vc-02 | exato | Abre VS Code no <projeto-2>. | abrir | <projeto-2> |
| vc-03 | exato | Abre VS Code no <projeto-3>. | abrir | <projeto-3> |
| vc-04 | irmão | Abre VS Code no <projeto-4>. | abrir | <projeto-4> |
| vc-05 | irmão | Abre VS Code no <projeto-5>. | abrir | <projeto-5> |
| vc-06 | irmão | Abre VS Code no <projeto-6>. | abrir | <projeto-6> |
| vc-07 | irmão | Abre VS Code no <projeto-7>. | abrir | <projeto-7> |
| vc-08 | irmão | Abre VS Code no <projeto-8>. | abrir | <projeto-8> |
| vc-09 | vocabulário | Abre VS Code no <projeto-9>. | abrir | <projeto-9> |
| vc-10 | vocabulário | Abre VS Code no <projeto-10>. | abrir | <projeto-10> |
| vc-11 | variante | Abre o Visual Studio Code na pasta <projeto-11>. | abrir | <projeto-11> |
| vc-12 | variante | Podes abrir o VS Code em <projeto-12>? | abrir | <projeto-12> |
| vc-13 | variante | Abre-me o Visual Studio no <projeto-13>, por favor. | abrir | <projeto-13> |
| vc-14 | negativo | Abre VS Code no projeto lontra. | nada | — |
| vc-15 | negativo | Abrir VS Code no girassol. | nada | — |
