# Guião de prompts para o Claude Code — português europeu

Frases inventadas que o Sponsor lê ao microfone com
`py -3.12 -m bench.record --set prompts` para medir o rato 5 (correção com o
contexto do projeto e enriquecimento do prompt) na voz real dele. Cada frase é
um pedido ao Claude Code de um dos projetos dele e tem pelo menos um termo do
domínio desse projeto, o tipo de palavra que o Whisper às vezes ouve mal. Só a
voz real conta; áudio sintético nunca entra nas medições.

Ficheiro versionado e público: nenhum nome de projeto nem termo real entra
aqui. Os projetos aparecem só como `<projeto-N>` e os termos do domínio como
`<termo-N>`; o gravador mostra no ecrã, no lugar deles, os nomes de
`[prompts.projects]` e os termos de `[prompts.terms]` da configuração local
`local/bench.toml` (ignorada pelo Git), escolhidos dos pacotes de contexto
desses projetos, e é isso que se lê em voz alta. As gravações ficam em
`local/recordings/prompts/` (ignorada).

Colunas (o carregador em `bench/prompts.py` valida-as):

- `caso`: `termo` (o termo é o que conta), `restrição` (o pedido tem uma
  restrição dita) ou `números` (o pedido tem números ditos);
- `intenção`: o que a frase pretende (`pedir`, `perguntar`, ...);
- `projeto`: o único `<projeto-N>` da frase;
- `termos`: os `<termo-N>` da frase, pela ordem em que aparecem, separados por
  vírgulas;
- `estilo`: sempre `claude-code` (o rato 5 envia para o Claude Code).

| id | caso | frase | intenção | projeto | termos | estilo |
|----|------|-------|----------|---------|--------|--------|
| pp-01 | termo | No <projeto-1>, vê porque é que a ligação ao <termo-1> falha logo de manhã e explica-me a causa. | pedir | <projeto-1> | <termo-1> | claude-code |
| pp-02 | números | Acrescenta ao <projeto-1> um teste para o <termo-2> que falhe quando o preço chega 5 segundos atrasado. | pedir | <projeto-1> | <termo-2> | claude-code |
| pp-03 | restrição | Corre outra vez a avaliação do <termo-3> no <projeto-1>, mas não mexas nos dados de treino nem no modelo. | pedir | <projeto-1> | <termo-3> | claude-code |
| pp-04 | termo | No <projeto-1>, o <termo-4> deixa de aparecer quando a <termo-5> fica cheia; descobre onde é que isso se perde. | pedir | <projeto-1> | <termo-4>, <termo-5> | claude-code |
| pp-05 | números | No <projeto-1>, passa a ler o <termo-2> de 30 em 30 segundos em vez de cada minuto e mostra-me o diff antes de gravares. | pedir | <projeto-1> | <termo-2> | claude-code |
| pp-06 | termo | No <projeto-2>, revê o lembrete da validação no <termo-6> e diz-me se a data que aparece está certa. | perguntar | <projeto-2> | <termo-6> | claude-code |
| pp-07 | restrição | Explica-me como está feito o limite do plano <termo-7> no <projeto-2>, sem alterares código nenhum. | perguntar | <projeto-2> | <termo-7> | claude-code |
| pp-08 | números | No <projeto-2>, as <termo-8> devem avisar 30 dias antes de acabar o prazo de 2 anos; verifica se isso já acontece. | pedir | <projeto-2> | <termo-8> | claude-code |
| pp-09 | termo | Faz com que o relatório anual do <projeto-2> também exporte em <termo-9> e acrescenta um teste para isso. | pedir | <projeto-2> | <termo-9> | claude-code |
| pp-10 | termo | No <projeto-3>, acrescenta o <termo-10> como fornecedor e usa o mesmo formato que os outros já usam. | pedir | <projeto-3> | <termo-10> | claude-code |
| pp-11 | números | Compara o custo do <termo-11> com o dos outros fornecedores do <projeto-3> nos últimos 7 dias e resume-me isso em 3 pontos. | pedir | <projeto-3> | <termo-11> | claude-code |
| pp-12 | restrição | Corre o <termo-12> no <projeto-3> e corrige só os avisos de formatação, sem mudar a lógica de nenhum ficheiro. | pedir | <projeto-3> | <termo-12> | claude-code |
| pp-13 | termo | No <projeto-4>, o parser falha com os ficheiros novos do <termo-13>; encontra a causa e propõe uma correção com testes. | pedir | <projeto-4> | <termo-13> | claude-code |
| pp-14 | restrição | Vê a pasta de <termo-14> do <projeto-4> e diz-me quantos ficheiros é que não foram importados, mas não apagues nada. | perguntar | <projeto-4> | <termo-14> | claude-code |
| pp-15 | números | No <projeto-4>, a estatística do <termo-15> 6 fica errada quando há 9 lugares ocupados; escreve um teste que mostre o erro. | pedir | <projeto-4> | <termo-15> | claude-code |
