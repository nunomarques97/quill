# Guião de instruções de reescrita — português europeu

Instruções inventadas que o Sponsor diz ao microfone com
`py -3.12 -m bench.record --set rewrite` para medir o modo comando do Quill:
com um texto selecionado, segura-se o gatilho de comando e diz-se o que fazer
com ele. Só a voz real dele conta; áudio sintético nunca entra nas medições.

Ficheiro versionado e público: todos os textos são inventados e nenhum nome
nem caminho real entra aqui. As gravações ficam em `local/recordings/rewrite/`
(ignorada pelo Git).

Ao gravar, o gravador mostra primeiro o texto selecionado (só para contexto,
não se lê) e depois a instrução, que é o que se diz em voz alta. A marcação de
hesitações e repetições é a do guião de ditado: `{hum}`, `{pronto}`, `{tipo}`,
`{é pá}` e `[repete] repete`.

Colunas (o carregador em `bench/rewrite.py` valida-as):

- `caso`: tipo de instrução. `encurtar` exige um texto mais curto e `lista`
  exige pelo menos dois itens começados por hífen; nos outros casos o texto
  tem de mudar;
- `frase`: a instrução falada;
- `seleção`: o texto selecionado que o modelo reescreve;
- `língua`: língua esperada do resultado, `pt` ou `en`;
- `preservar`: termos que têm de aparecer no resultado, separados por `;`,
  ou `—` quando não há.

| id | caso | frase | seleção | língua | preservar |
|----|------|-------|---------|--------|-----------|
| rw-01 | formal | Põe isto mais formal. | olá, amanhã não dá para a reunião das dez, podemos passar para quinta? | pt | quinta |
| rw-02 | inglês | Traduz para inglês. | O deploy de sexta correu bem, mas o dashboard ainda mostra os dados de ontem. | en | deploy; dashboard |
| rw-03 | encurtar | Encurta isto para uma frase só. | Queria avisar que a entrega da encomenda foi adiada porque o fornecedor teve um problema com o transporte. Em princípio chega na próxima terça-feira, mas ainda vão confirmar a hora por telefone. | pt | terça |
| rw-04 | corrigir | Corrige os erros de ortografia. | Amanha vou enviar o relatorio com as conclusoes da analise. | pt | amanhã; relatório; conclusões; análise |
| rw-05 | informal | {hum} deixa isto mais informal, é para um amigo. | Informo que não poderei comparecer ao jantar de sábado. | pt | sábado |
| rw-06 | inglês | Traduz isto para inglês, por favor. | Por favor, revê o pull request antes do merge e confirma que os testes passam. | en | pull request; merge |
| rw-07 | português | Traduz para português de Portugal. | Can you send me the invoice by Friday? The budget review is on Monday. | pt | — |
| rw-08 | lista | Transforma isto numa lista com hífenes. | Para o fim de semana falta comprar pão, leite, ovos e café. | pt | pão; leite; ovos; café |
| rw-09 | formal | Põe mais formal, é para um cliente. | Olha, o orçamento sobe 10% se quiserem a app também em iOS. | pt | 10%; iOS |
| rw-10 | encurtar | Resume isto em poucas palavras. | A nova versão da aplicação arranca mais depressa, gasta menos bateria e já não perde as definições quando o telemóvel reinicia, o que resolve as queixas mais frequentes dos utilizadores. | pt | — |
| rw-11 | corrigir | Corrige a pontuação. | então ficou combinado o backup corre todas as noites às duas e o relatório chega de manhã | pt | backup |
| rw-12 | simplificar | Diz isto de forma mais simples. | Em virtude da indisponibilidade do servidor principal, procedeu-se à ativação do sistema de contingência. | pt | — |
| rw-13 | inglês | {pronto} passa isto para inglês e mantém os termos técnicos. | O endpoint devolve um erro 500 depois de o token expirar e é preciso limpar a cache. | en | endpoint; 500; token; cache |
| rw-14 | simpático | Torna isto mais simpático. | Ainda não recebi o ficheiro. Mandem hoje. | pt | ficheiro |
| rw-15 | formal | Reescreve em tom profissional. | preciso das faturas de março até sexta senão não consigo fechar as contas | pt | março |
| rw-16 | encurtar | {tipo} corta isto para metade. | Na reunião da semana passada decidimos que a equipa de suporte passa a responder aos pedidos urgentes em duas horas e aos restantes até ao fim do dia seguinte, a partir do próximo mês. | pt | — |
| rw-17 | lista | Faz uma lista com os passos. | Primeiro instala as dependências, depois corre as migrações do esquema e por fim arranca o servidor. | pt | — |
| rw-18 | português | Traduz isto para português. | Please restart the server after the update and check that the backup finished. | pt | backup |
| rw-19 | inglês | Traduz para inglês em tom informal. | Obrigado pela ajuda de ontem, fico a dever-te um café! | en | — |
| rw-20 | corrigir | Corrige a concordância, [das frases] das frases. | Os resultado do teste mostra que as alteração funciona bem. | pt | resultados; alterações |
