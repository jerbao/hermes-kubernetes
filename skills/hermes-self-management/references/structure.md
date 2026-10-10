# Estrutura em uso

Consulte `python3 hermes.py runtime info`, a política da instância e
lock de upstream da base pública para caminhos e versões atuais. Valores concretos da
infraestrutura pertencem aos manifestos; não os fixe em scripts genéricos nem
trate documentação antiga de migração como descrição da produção atual.

| Componente | Função e persistência |
|---|---|
| Terminal SSH | Shell, projetos, Docker e desktop; home/workspace persistente em storage persistente independente do sistema operacional |
| Pod Hermes, namespace da instância | Imagem AlmaLinux x86-64-v2; estado ativo no PVC; quatro containers abaixo |
| `gateway` | Canais, sessões e agente; único gateway pessoal de produção |
| `dashboard` | API usada pelo Desktop, plugins, Connectors e navegador de arquivos por SSH |
| `diagnostics` | Copia proveniência e logs selecionados para a área privada do terminal |
| `operations` | Ponte privada iniciada pelo pod via SSH, com socket no terminal |
| MCP geral (`api`) | Pod Alpine, pacotes stdio e PVC próprios; sem acesso geral ao workspace/estado Hermes |
| MCP navegador (`browser`) | Pod Alpine separado, dependências, dados e transporte conforme a política |
| Codegraph (`workspace`) | Pod AlmaLinux/glibc, índices próprios e mounts autorizados dos projetos compartilhados |
| CLIProxyAPI | Namespace próprio `cli-proxy-api`, credenciais/PVC próprios; configurado pela instância |
| Tailscale/rede | Integração do cluster ou tailnet dedicada; NetworkPolicies e listas de destinos nos manifestos |

O terminal substituiu a instância anterior; não volte a iniciar seu gateway. Recriar o
sistema operacional não autoriza excluir home persistente ou PVCs. Persistência
protege contra recriação; recuperação de exclusões exige backup.

## Caminhos e repositórios

No terminal, `~/.hermes` é a raiz Git de repositório privado. `hermes-agent/` é
checkout independente reconstruído do upstream fixado mais patches. A base
pública fica em `.local/platform/<revision>/`, selecionada por `platform.lock.json`;
essa dependência ficam runtime, scripts e patches compartilhados. `.local/`
contém diagnósticos, ambientes de desenvolvimento e estado privado da
sincronização. Cópias/projeções no terminal não substituem o estado ativo do pod.

Na ponte, `state` corresponde ao HERMES_HOME ativo, `source` ao núcleo da imagem
e `integration` à distribuição. `info` informa os caminhos; na imagem atual
são `/opt/data`, `/opt/hermes` e `/opt/hermes-personal`. Os últimos dois são
somente leitura. O socket padrão no terminal fica em
`~/.hermes/.local/runtime/control.sock`; `--socket` seleciona outro socket.

| Repositório | Responsabilidade |
|---|---|
| repositório privado | Política da instância, personalizações sanitizadas, locks e referência à release pública |
| manifestos privados da instância | Deployment, PVCs, executores MCP, rede, Secrets selados e CronJobs de backup |
| manifestos privados do proxy | Proxy pessoal e acesso por clientes autorizados |
| manifestos privados do ArgoCD | Applications ArgoCD |
| repositório privado de infraestrutura | Cluster, Proxmox, criação/recriação do terminal e mounts persistentes |

O terminal tem permissões do usuário e acesso à ponte, sem credencial
administrativa do Kubernetes. Não copie kubeconfig ou credenciais Proxmox
para o agente como atalho.

## Estado e perfis

Prefira interface/ferramenta nativa. Pela ponte, `list`, `stat`, `get` e `put`
operam no estado real. Para edição manual, baixe uma cópia privada, revise e
envie com o hash anterior; `absent` é somente para criar arquivo novo:

```sh
python3 hermes.py runtime stat config.yaml --sha256
python3 hermes.py runtime get config.yaml --output /tmp/config-pod.yaml
python3 hermes.py runtime put config.yaml --input /tmp/config-pod.yaml --expected SHA256_ANTERIOR
```

No escopo `state`, `config.yaml` é do default; os demais perfis ficam em
`profiles/PERFIL/`. Cada perfil mantém configuração, plugins e `.env` próprios.
Não compartilhe `.env` por symlinks. Para CLI, use sempre
`hermes --profile PERFIL ...` dentro de `runtime_client.py exec`.

SQLite/WAL/SHM não podem ser transferidos ou sobrescritos como arquivos comuns.
Use `sqlite-backup` para copiar banco ativo. O navegador de arquivos da GUI
usa SSH ao terminal; suas pastas não demonstram onde um plugin backend foi instalado.
Host, usuário, chave e cwd precisam da política efetiva do perfil, não somente
das variáveis do container.

## MCPs

Configure pelo Connectors/API nativa do perfil. HTTP acessa o serviço remoto;
stdio é encaminhado pelo hook `mcp.stdio_executor` aos executores isolados.
Pacotes são preparados nesses executores, sem instalar dependências no Python
do Hermes ou no terminal.

Para um pacote novo, avalie suas necessidades e indique em `args` quando
necessário: `--hermes-executor=api`, `--hermes-executor=workspace` ou
`--hermes-executor=browser`. A bridge remove o flag antes de iniciar o MCP.
A seleção automática usa identidades/pacotes do mapa e caminhos autorizados;
não infere necessidades arbitrárias de um pacote desconhecido. `vm` exige
alvo/chave MCP dedicados provisionados; não é fallback para a chave do terminal.
Não suponha que esse alvo esteja habilitado.

```sh
python3 hermes.py runtime exec --scope integration --timeout 3600 -- \
  /opt/hermes/.venv/bin/python /opt/hermes-personal/mcp_update.py plan --all
```

Após revisar o plano, use `apply NOME` para um MCP ou `apply --all` quando uma
atualização geral foi solicitada. Falha de instalação/handshake conserva a
geração anterior. HTTP é administrado pelo provedor. Detalhes de roteamento
ficam em `docs/capabilities-interface.md`.

## Git, atualizações e backups

O controlador no terminal reconcilia Git, estado nativo e baseline, preservando
conflitos. Projeta fontes de skills/plugins, persona, configuração com
referências privadas, origem/revisão e locks PM/MCP em `customizations/<perfil>/`.
Em `main`, publica alterações admitidas após 120 segundos estáveis. Branches,
conflitos e mudanças da versão da distribuição exigem PR e tratamento explícito.
Veja `docs/versionamento.md`; editar a projeção não prova aplicação no pod.

Personalizações não exigem rebuild. Código da distribuição/núcleo passa por
CI da base pública, release, PR privado, digest nos manifestos e ArgoCD. Releases são fixadas no lock e atualizadas
pelo fluxo versionado; não use `git pull` do upstream nem `hermes update` no
código somente leitura do pod para atualizar produção.

Skills oficiais com patches e skills da distribuição usam reconciliação com
três versões: base anterior, fonte candidata e arquivos do perfil. A preparação
da release registra o resultado em `skill-update.json`, incorpora personalizações
compatíveis à PR e bloqueia publicação em caso de conflito, com alerta no GitHub.
O runtime aplica um plano completo antes de iniciar; nenhum arquivo em conflito
é escrito nas skills ativas. As bases e diagnósticos são privados em
`state/versioning/managed-skills/`. Skills removidas pelo usuário permanecem
removidas; skills retiradas do upstream são preservadas como pessoais.

Arquivos iguais às fontes gerenciadas são reconstruídos da imagem, sem cópias
redundantes em `customizations`. Edições de perfil continuam bidirecionais. O
controlador aguarda a imagem com os hashes de fonte e distribuição correspondentes
antes de aplicar uma projeção que dependa dessa atualização. Não tente contornar
`waiting-for-runtime` instalando o núcleo ou skills no terminal.

Git e imagens não guardam SQLite, histórico, credenciais ou dados pessoais.
Referências privadas são resolvidas pelo cofre no PVC. Estado Hermes, workspace
e dados dos executores têm backups próprios criptografados no Google Drive,
com seleção nos manifestos. Não arquive todo o home/cache por padrão nem inicie
backup completo por operação. Consulte `docs/backup-google-drive.md` e
`docs/backup-workspace.md` quando houver pedido de backup/restauração; restaure
em área vazia e coordene escritores antes de trocar o estado de produção.
