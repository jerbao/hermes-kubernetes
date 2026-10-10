# Executor separado dos MCPs

Imagem própria com Node, Python e as versões dos MCPs encontradas na VM de
referência. `package-lock.json` fixa também as dependências transitivas. O
processo não instala pacotes no runtime do Hermes nem na VM do terminal.

O Hermes mantém suas configurações nativas. A ponte stdio abre uma sessão SSH
autenticada no serviço do executor e envia a definição privada pelo stdin;
depois encaminha os bytes MCP sem alterar o protocolo. A host key é verificada
e a chave do cliente é separada da usada no terminal. HTTP MCP permanece HTTP.

O executor roda sem root e sem acesso ao PVC do Hermes. Aceita somente o comando
SSH `hermes-mcp`; não publica shell, SFTP ou encaminhamento de portas. Cada
perfil tem HOME e caches próprios. Isso preserva os caminhos por perfil, mas
não constitui isolamento de segurança entre processos do mesmo pod e UID.

Os comandos `npx` dos pacotes conhecidos usam os binários já instalados na
imagem até que o administrador aprove um override persistente do perfil.
Pacotes `npx` e `uvx` também podem ser atualizados sem reconstruir a imagem,
invocando `/opt/hermes-personal/mcp_update.py` via `vm/runtime_client.py exec`
no container Hermes: `list NOME`, `plan NOME`, `apply NOME` ou `apply --all`.
`--version X` solicita uma versão explícita para um MCP; sem ela, specs
flutuantes são atualizadas e specs explicitamente fixadas são puladas.
Definições são lidas do `config.yaml` atual do perfil. O snapshot JSON
`state/hermes-ssh-mcp.json` só restaura a definição original dos servidores que
continuam usando a bridge; `--definitions ARQUIVO` seleciona explicitamente um
snapshot alternativo. A conexão usa a chave MCP dedicada, política administrativa
e host key verificada.

O executor prepara uma geração em staging, executa o handshake MCP
`initialize` e a lista de recursos que o servidor anuncia, sem modelo, e só então
grava o ponteiro ativo do perfil. A atualização não encerra processos MCP de
conexões já abertas: a nova geração entra em uso na próxima conexão/reconexão.
Falha mantém a geração anterior. Locks npm e versões Python resolvidas ficam no
volume durável por perfil; arquivos instalados e caches ficam em `/tmp` e são
reconstruídos do lock no próximo uso. `--all` retorna estado individual de cada
servidor e marca HTTP remoto como `skipped-http`, já que o provider controla essa
versão. Credenciais, configurações, índices e dados do servidor não são alterados.

Por padrão não há workspace montado. Um MCP que declare `cwd` exige um mount
de projetos explícito por `--workspace`; caminhos externos e links que escapem
da área montada são recusados. Codegraph precisa dos projetos e índices, e o
navegador precisa do backend e de seus dados persistentes. Instalar os pacotes
não demonstra equivalência dessas ferramentas; os testes funcionais precedem
a adoção, conforme `docs/terminal-descartavel.md`.

```sh
python3 -m venv /tmp/hermes-mcp-tests
/tmp/hermes-mcp-tests/bin/pip install -r mcp/requirements.txt
make MCP_PYTHON=/tmp/hermes-mcp-tests/bin/python test-mcp
docker build --platform linux/amd64 -t hermes-mcp:local mcp
```

O CI testa a interface SSH e a imagem sem root, publica a imagem própria e
publica os digests na release para adoção pelos manifestos privados. Não baixa nem testa o núcleo
upstream do Hermes. Deployment, serviço, rede, PVCs e SealedSecrets pertencem a
os manifestos privados da instância.
