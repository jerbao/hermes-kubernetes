# Plugins no perfil e no runtime corretos

Os exemplos partem de `~/.hermes` **no terminal terminal**. A ponte executa a CLI
no container `operations`, com usuário do Hermes e estado do PVC.
`--profile` seleciona o perfil antes de importar as rotinas; omiti-lo pode
seguir a seleção persistida de outro perfil.

## Instalar e conferir

Use o identificador solicitado: nome do catálogo, URL Git ou `owner/repo`.
Para uma revisão específica, `--ref` aceita SHA Git completo de 40 caracteres.

```sh
python3 hermes.py runtime exec --timeout 3600 -- \
  hermes --profile default plugins install IDENTIFICADOR --yes-deps --enable
python3 hermes.py runtime exec -- \
  hermes --profile default plugins list --user --json
```

`exec` não fornece stdin interativo. `--yes-deps` responde ao consentimento
nativo para instalar as dependências incluídas no pedido; `--enable` pede
ativação. Para instalar sem ativar, use `--no-enable`. `--no-deps` deixa o plugin
desabilitado e sem dependências; não reporte esse modo como instalação funcional.

Plugins portáveis Agent Plugins v1 têm regras próprias de admissão e podem
permanecer desabilitados. Use o nome/chave retornado, confira o registro e,
quando necessário, ative explicitamente no mesmo perfil:

```sh
python3 hermes.py runtime exec -- hermes --profile default plugins enable NOME
```

Preserve o erro e explique recusas de admissão, permissões ou substituição de
ferramentas. Não acrescente `--force`, `--allow-removed` ou
`--allow-tool-override` para contorná-las silenciosamente. Não declare sucesso
quando apenas o código foi baixado. Se necessário, teste somente a função do
plugin instalado, sem repetir outros recursos.

## Outras operações

```sh
python3 hermes.py runtime exec -- hermes --profile default plugins disable NOME
python3 hermes.py runtime exec -- hermes --profile default plugins enable NOME
python3 hermes.py runtime exec -- hermes --profile default plugins check-updates --json
python3 hermes.py runtime exec --timeout 3600 -- hermes --profile default plugins update NOME
python3 hermes.py runtime exec -- hermes --profile default plugins remove NOME
```

`check-updates` consulta; `update NOME` altera apenas o plugin escolhido.
Catálogo segue seus pins; uma instalação Git fixada por SHA exige escolher
explicitamente a nova revisão, conforme a mensagem nativa. Não atualize plugins
fazendo `git pull` indiscriminadamente em seus diretórios.
Se a CLI exigir confirmação indisponível por stdin, use a ação equivalente
da interface/API nativa com seu fluxo de consentimento; não simule respostas
nem altere os arquivos internos do gerenciador para contornar a recusa.

Configuração e ativação continuam em **Capabilities → Plugins**. O JSON-RPC
nativo usa `plugins.manage` com ações `list`, `install`, `toggle`, `settings`,
`update` e `remove`, no escopo autenticado do perfil e com parâmetros do
contrato instalado. A ponte de operações não é um endpoint JSON-RPC.

## Ambiente e ativação

O código vive em `HERMES_HOME/plugins` do perfil; o PM mantém ferramentas,
locks e gerações persistentes no PVC. Não use pip global, a venv do checkout
de debug ou instalações efêmeras em `/opt/hermes`.

Instalar/ativar pode recarregar hooks. Ferramentas e prompts seguem a regra
nativa de nova sessão; desativar não desfaz necessariamente handlers carregados.
Respeite `restart_required` e informe o que falta para a sessão em uso.
Kubernetes supervisiona o gateway: não inicie um segundo gateway no terminal, não
mate processos pelo nome nem use seu systemd para reiniciar o pod. Reiniciar
o deployment é ação de administração do cluster.

Um plugin com metade visual para Electron pode exigir instalação adicional no
Hermes Desktop do computador do usuário. Instalar backend não instala esse
componente local. Plugins que dependem de display, Docker, caminhos locais ou
processos externos precisam ser compatíveis com a divisão terminal/pod; avalie as
dependências declaradas antes de prometer funcionamento.

O controlador registra configuração sanitizada, fontes, revisões e locks em
`customizations/<perfil>/`. Credenciais, bancos e dados pessoais continuam no
estado privado e nos backups criptografados. Esse registro não substitui o
gerenciador nativo.
