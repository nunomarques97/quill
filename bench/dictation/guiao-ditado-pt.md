# Guião de ditado real — português europeu

Frases inventadas que o Sponsor lê ao microfone com `py -3.12 -m bench.record`
para medir o Quill em ditado real: frases de 5 a 30 segundos, com termos em
inglês, hesitações e repetições naturais. Só a voz real dele conta; áudio
sintético nunca entra nas medições.

Ficheiro versionado e público: nenhum nome nem caminho real entra aqui. Os
projetos aparecem só como `<projeto-1>` e `<projeto-2>`; o gravador mostra no
ecrã os nomes da configuração local (ignorada pelo Git) no lugar deles, e é
isso que se lê em voz alta. As gravações ficam em `local/recordings/dictation/`
(ignorada).

Marcação dentro da `frase`:

- `{hum}`, `{pronto}`, `{tipo}`, `{é pá}`: hesitação que se diz em voz alta e
  que a limpeza deve remover. O gravador mostra-a como `hum…`.
- `[abre o] abre o`: repetição; diz-se o texto entre parênteses retos, uma
  pausa curta, e depois repete-se. A limpeza deve remover a primeira vez.
- `tipo` e `pronto` sem chavetas têm significado e ficam no texto final.

Colunas (o carregador em `bench/dataset.py` valida-as):

- `caso`: duração aproximada, `curto` (5 a 10 s), `médio` (10 a 20 s) ou
  `longo` (20 a 30 s);
- `intenção`: o que a frase pretende (`pedir`, `perguntar`, `ditar`,
  `informar`, `combinar`, ...);
- `projeto`: marcador esperado, ou `—` quando a frase não nomeia projeto;
- `ativação`: sempre `não` (o ditado nunca envia a mensagem);
- `estilo`: janela de destino, `claude-code`, `vscode`, `whatsapp` ou `email`.

| id | caso | frase | intenção | projeto | ativação | estilo |
|----|------|-------|----------|---------|----------|--------|
| dt-01 | curto | {hum} corre os testes do <projeto-1> e diz-me quais é que falham, sem mudar nada no código. | pedir | <projeto-1> | não | claude-code |
| dt-02 | médio | Olha, eu quero que leias o readme do <projeto-2> e {tipo} que me faças um resumo do setup, [com os] com os passos que faltam para correr o build em Windows. | pedir | <projeto-2> | não | claude-code |
| dt-03 | longo | {pronto} então, há um bug no login do backend, [quando o] quando o token expira a página fica em branco. Quero que vejas os logs, que encontres a causa e que proponhas uma correção com testes, mas {hum} não faças commit nem push sem eu rever primeiro. | pedir | — | não | claude-code |
| dt-04 | médio | Faz um debug ao script de deploy, {é pá} está a falhar no passo do upload e eu não percebo porquê. Mostra-me o erro exato e o ficheiro onde acontece. | pedir | — | não | claude-code |
| dt-05 | médio | {hum} [cria uma] cria uma task nova no <projeto-1> para migrar o dashboard para o frontend novo e divide-a em passos pequenos que eu possa rever um a um. | pedir | <projeto-1> | não | claude-code |
| dt-06 | curto | Qual é a diferença entre fazer merge e fazer rebase neste caso, {tipo} qual deles é mais seguro para o histórico? | perguntar | — | não | claude-code |
| dt-07 | longo | Preciso de um workflow do GitHub que corra os testes em cada pull request e que {hum} publique o relatório como comentário. [Usa o] Usa o mesmo JSON de configuração que já temos e explica-me no fim o que faz cada job, {pronto}, de forma curta. | pedir | — | não | claude-code |
| dt-08 | médio | Revê este prompt antes de o usarmos no <projeto-2>, {é pá} acho que está demasiado longo e [que as] que as instruções se contradizem na parte do formato da resposta. | pedir | <projeto-2> | não | claude-code |
| dt-09 | curto | Faz update das dependências do frontend e corre o benchmark outra vez para ver se ficou mais lento. | pedir | — | não | claude-code |
| dt-10 | curto | {hum} Comentário: esta função devolve a lista vazia quando a API não responde dentro do tempo limite. | ditar | — | não | vscode |
| dt-11 | médio | Mensagem de commit: corrige o cálculo das datas no relatório mensal e [acrescenta um] acrescenta um teste para o mês de fevereiro em anos bissextos. | ditar | — | não | vscode |
| dt-12 | médio | TODO, {tipo}, rever o tratamento de erros no backend, porque neste momento os logs guardam a mensagem inteira e isso pode incluir dados pessoais dos clientes. | ditar | — | não | vscode |
| dt-13 | longo | Nota para o readme do <projeto-1>: para fazer o setup basta clonar o repositório, criar o ambiente virtual e correr o script de instalação. {pronto} [Se o] Se o build falhar no Windows, confirmem primeiro a versão do Python e só depois abram uma issue no GitHub. | ditar | <projeto-1> | não | vscode |
| dt-14 | curto | {é pá} renomeia a variável total para total de linhas processadas em todo o ficheiro e nos testes também. | pedir | — | não | vscode |
| dt-15 | médio | Documentação da função: recebe o caminho do ficheiro JSON, valida o esquema e devolve {hum} um dicionário com as opções. [Se o] Se o ficheiro não existir, lança um erro com uma mensagem clara. | ditar | — | não | vscode |
| dt-16 | médio | Mensagem de commit: {hum} altera o tipo de dados da coluna de preços para decimal e atualiza a migração da base de dados. | ditar | — | não | vscode |
| dt-17 | longo | No ficheiro de configuração do <projeto-2> quero adicionar uma hotkey para abrir o painel de logs, {tipo} control shift L, e [quero que] quero que o atalho apareça também no menu de ajuda. {pronto} depois atualiza o changelog com uma linha a explicar a alteração. | pedir | <projeto-2> | não | vscode |
| dt-18 | curto | Comentário: o pedido é repetido três vezes antes de desistir, com uma pausa de dois segundos entre tentativas. | ditar | — | não | vscode |
| dt-19 | curto | {é pá} já estou a sair do escritório, chego aí [em vinte] em vinte minutos, guarda-me um lugar. | combinar | — | não | whatsapp |
| dt-20 | médio | Olá! {hum} amanhã não vou conseguir ir ao jantar, tenho uma entrega do projeto na sexta e ainda falta muita coisa. Combinamos para a semana? | combinar | — | não | whatsapp |
| dt-21 | curto | O jantar está pronto, desce quando puderes e traz o pão que ficou em cima da mesa. | pedir | — | não | whatsapp |
| dt-22 | médio | {tipo} vi agora a tua mensagem, [não te] não te preocupes com isso, eu trato do bilhete e depois tu pagas-me quando der. | informar | — | não | whatsapp |
| dt-23 | longo | Olha, {pronto}, o meu portátil morreu outra vez e perdi a manhã toda. [Achas que] Achas que o teu primo ainda arranja computadores? Se sim, passa-me o número dele que eu ligo-lhe hoje à tarde, {hum} antes das seis. | pedir | — | não | whatsapp |
| dt-24 | curto | Parabéns pelo novo emprego! {é pá} merecias mesmo, temos de festejar isso num destes fins de semana. | felicitar | — | não | whatsapp |
| dt-25 | médio | Consegues ver se o <projeto-1> está online? {hum} a mim não abre e o cliente está sempre a mandar mensagens a perguntar [o que] o que é que se passa. | perguntar | <projeto-1> | não | whatsapp |
| dt-26 | médio | {tipo} ontem fomos àquele restaurante novo perto do rio e, sinceramente, [a comida] a comida era boa mas o serviço foi muito lento. | informar | — | não | whatsapp |
| dt-27 | curto | Já mandei o email com o orçamento, depois diz-me o que achas quando tiveres um minuto. | informar | — | não | whatsapp |
| dt-28 | médio | Bom dia, {hum} envio em anexo a proposta revista para a segunda fase do <projeto-2>. [Fico a] Fico a aguardar o vosso feedback até ao final da semana. Com os melhores cumprimentos. | enviar | <projeto-2> | não | email |
| dt-29 | longo | Olá a todos, {pronto}, na reunião de ontem ficou decidido que o deploy passa para a próxima quinta-feira. [Até lá] Até lá precisamos de fechar os testes de carga e de atualizar a documentação da API. Se alguém tiver algum impedimento, {hum} avise-me por favor até amanhã. | informar | — | não | email |
| dt-30 | médio | Boa tarde, {hum} peço desculpa pela demora na resposta. [Estive a] Estive a rever o contrato e tenho duas dúvidas sobre os prazos de pagamento que gostava de esclarecer por telefone. | responder | — | não | email |
| dt-31 | curto | Obrigado pelo envio dos ficheiros, {hum} vou analisar tudo com calma e respondo até sexta-feira. | agradecer | — | não | email |
| dt-32 | longo | Exmos. Senhores, venho por este meio pedir um orçamento para a manutenção anual do sistema de faturação. {tipo} precisamos de suporte em horário laboral, [de uma] de uma atualização por trimestre e de um relatório mensal com os incidentes. Agradeço desde já a atenção. | pedir | — | não | email |
| dt-33 | médio | Olá, {hum} segue o link para o dashboard com os números de setembro. {pronto} os valores ainda são provisórios, por isso não os partilhem fora da equipa por enquanto. | informar | — | não | email |
| dt-34 | médio | Boa tarde, confirmo a reunião de terça às dez horas na sala grande. [Vou levar] Vou levar o portátil com a demonstração do <projeto-1> e {tipo} uma cópia impressa do plano. | confirmar | <projeto-1> | não | email |
| dt-35 | longo | Cara equipa, {hum} depois do incidente de sábado revimos o processo de backup e encontrámos duas falhas. [A primeira] A primeira é que os logs não eram guardados fora do servidor e a segunda é que ninguém recebia o alerta por email. Ambas ficam corrigidas esta semana. | informar | — | não | email |
| dt-36 | curto | Olá, anexo a fatura de agosto, que ficou esquecida no último envio. As minhas desculpas pelo atraso. | enviar | — | não | email |
