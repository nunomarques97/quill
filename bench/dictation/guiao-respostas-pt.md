# Guião de respostas ao Claude Code — português europeu

Respostas curtas inventadas que o Sponsor lê ao microfone com
`py -3.12 -m bench.record --set replies` para medir o rato 5 com a última
mensagem do Claude Code como contexto, na voz real dele. Cada frase responde a
uma mensagem típica do Claude Code: um sim ou um não com uma condição, a
escolha de uma opção numerada, a escolha de um ficheiro, a correção de um termo
ou o pedido para continuar ou parar. Só a voz real conta; áudio sintético nunca
entra nas medições.

Ficheiro versionado e público: nenhum nome de projeto, termo real nem texto do
Claude entra aqui. Os projetos aparecem só como `<projeto-N>` e os termos do
domínio como `<termo-N>`; o gravador mostra no ecrã, no lugar deles, os nomes
de `[replies.projects]` e os termos de `[replies.terms]` da configuração local
`local/bench.toml` (ignorada pelo Git), e é isso que se lê em voz alta. A coluna
`mensagem do Claude` só descreve, de forma genérica, a mensagem a que a frase
responde: a mensagem real fica em `local/replies/rr-NN.md` (ignorada). As
gravações ficam em `local/recordings/replies/` (ignorada). Os passos estão em
`docs/research/GRAVAR-RESPOSTAS.md`.

Colunas (o carregador em `bench/prompts.py` valida-as):

- `caso`: `sim-condição`, `opção`, `ficheiro`, `termo`, `continuar` ou `parar`;
- `intenção`: o que a frase pretende (`responder`, `escolher`, `corrigir`, ...);
- `projeto`: o `<projeto-N>` da janela do Claude Code a que se responde (não se
  diz em voz alta);
- `termos`: os `<termo-N>` da frase, pela ordem em que aparecem, separados por
  vírgulas, ou `—` quando a frase não tem nenhum;
- `mensagem do Claude`: descrição genérica da mensagem a que a frase responde;
- `estilo`: sempre `claude-code` (o rato 5 envia para o Claude Code).

| id | caso | frase | intenção | projeto | termos | mensagem do Claude | estilo |
|----|------|-------|----------|---------|--------|--------------------|--------|
| rr-01 | sim-condição | Sim, podes aplicar, mas só depois de os testes do <termo-1> passarem todos. | responder | <projeto-1> | <termo-1> | Propõe uma alteração e pergunta se a pode aplicar. | claude-code |
| rr-02 | sim-condição | Ainda não; primeiro mostra-me o diff do <termo-2> e depois eu decido. | responder | <projeto-1> | <termo-2> | Pergunta se pode gravar as alterações feitas. | claude-code |
| rr-03 | opção | Vai pela opção dois, a que só mexe no <termo-3>. | escolher | <projeto-2> | <termo-3> | Oferece três opções numeradas para resolver um problema. | claude-code |
| rr-04 | opção | Prefiro a primeira abordagem, desde que não apagues os dados antigos. | escolher | <projeto-2> | — | Compara duas abordagens com vantagens e desvantagens e pergunta qual seguir. | claude-code |
| rr-05 | ficheiro | Começa pelo ficheiro do <termo-4> e deixa os restantes para depois. | escolher | <projeto-1> | <termo-4> | Lista vários ficheiros afetados e pergunta por qual começar. | claude-code |
| rr-06 | termo | Não é <termo-5>, é <termo-6>; acerta isso no resumo. | corrigir | <projeto-2> | <termo-5>, <termo-6> | Resume o trabalho feito e usa um termo trocado. | claude-code |
| rr-07 | termo | O nome certo é <termo-7>, escrito como está no código. | corrigir | <projeto-1> | <termo-7> | Explica uma parte do código e escreve mal o nome de um componente. | claude-code |
| rr-08 | continuar | Continua com o resto da lista e avisa-me quando chegares ao <termo-8>. | continuar | <projeto-2> | <termo-8> | Para a meio de uma lista de tarefas e pergunta se continua. | claude-code |
| rr-09 | parar | Para por aqui e não mexas em mais nada até eu rever o que fizeste. | parar | <projeto-1> | — | Acaba um passo e pergunta se avança para o seguinte. | claude-code |
| rr-10 | continuar | Sim, repete a mesma mudança nos outros 3 sítios e no fim volta a correr tudo. | continuar | <projeto-2> | — | Pergunta se aplica a mesma alteração aos restantes sítios onde ela aparece. | claude-code |
