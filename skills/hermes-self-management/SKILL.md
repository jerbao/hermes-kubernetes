---
name: hermes-self-management
description: Administrar o próprio Hermes nesta implantação Kubernetes, incluindo plugins, skills, MCPs, perfis, configuração, atualizações e debug, usando o runtime ativo a partir do terminal separado.
---

O terminal está em um **terminal SSH**, enquanto gateway e dashboard/API executam
no Kubernetes. Um comando comum no terminal executa no terminal, não no pod.
`~/.hermes` no terminal é a raiz Git do projeto; `HERMES_HOME` no pod é o estado
ativo no PVC. Caminhos semelhantes e sincronização não os tornam o mesmo runtime.

## Escolha do destino

| Pedido | Onde agir |
|---|---|
| Instalar, atualizar, ativar, desativar ou remover plugin | Gerenciador nativo **no pod**, via interface ou ponte de operações |
| Configurar perfil, credenciais, canais, skills ou MCPs | Estado nativo do perfil **no pod**, pela interface/ferramenta nativa ou ponte |
| Executar shell, trabalhar em projetos, usar desktop | Terminal terminal e workspace persistente |
| Instalar pacotes de um MCP stdio | Executor MCP escolhido pela política, pelo transporte nativo |
| Corrigir o núcleo do Hermes | Checkout `hermes-agent/` no terminal, exportar patches e publicar imagem |
| Mudar rede, volumes, serviços ou infraestrutura | Repositório responsável; o terminal não administra o cluster |

Antes de administrar o estado ativo:

```sh
cd ~/.hermes
python3 hermes.py runtime info
```

Confira instância, caminhos e proveniência retornados. Se a ponte estiver
indisponível, reporte isso; não substitua a operação por uma instalação local.
Use explicitamente o perfil do pedido. `default` é um perfil válido, inclusive
quando há um único bot. Não troque o perfil global como atalho.

## Plugins: usar o runtime ativo

Leia [plugins e perfis](references/plugins.md) antes de administrar um plugin
por conversa. Use a CLI nativa através da ponte, por exemplo:

```sh
python3 hermes.py runtime exec --timeout 3600 -- \
  hermes --profile default plugins install IDENTIFICADOR --yes-deps --enable
```

Substitua `default` pelo perfil do pedido e `IDENTIFICADOR` pela entrada do
catálogo ou repositório solicitado. Os flags acima correspondem ao pedido de
instalar com dependências e ativar; para somente instalar, use `--no-enable`.
Preserve recusas e consentimentos nativos. Confira o registro no **mesmo perfil**,
o resultado de ativação e eventual necessidade de nova sessão ou reinício.

**Não execute `hermes plugins install`, `pip install` ou `git clone` diretamente
no terminal para administrar produção.** Não instale pacotes no código somente leitura
da imagem nem reconstrua a imagem para cada plugin. O PM nativo prepara as
dependências persistentes no PVC. A referência explica plugins portáveis,
atualizações e componentes que também precisam ser instalados no Desktop local.

## Estrutura e personalizações

Leia [estrutura atual](references/structure.md) para localizar containers,
volumes, executores MCP, inferência, repositórios, sincronização e backups.
Ela explica qual cópia de um arquivo editar e como verificar publicação.

Crie skills pessoais pela ferramenta `skill_manage` no perfil ativo. Skills e
plugins instalados são projetados automaticamente em `customizations/<perfil>/`,
sem editar uma lista por nome. A base pública seleciona as skills **da distribuição**; overrides
pessoais continuam no repositório privado. Uma edição feita apenas no terminal não comprova
instalação nem recarga no pod. Cada perfil mantém seu próprio `.env`.

`skills/` na base pública é a fonte das skills da distribuição; não mantenha cópias idênticas
manualmente em `customizations/`. Alterações nessa fonte passam por CI/imagem.
A atualização compara a base anterior, a nova fonte e a personalização do perfil;
preserva edições compatíveis e bloqueia conflitos antes de publicar a candidata.
As cópias instaladas são reconciliadas no runtime. Diagnósticos ficam fora das
skills carregadas; nunca entregue marcadores de conflito ao agente. Veja
`docs/atualizacoes.md` e `scripts/reconcile_skills.py` para preparar uma candidata.

## Desenvolvimento e debug do núcleo

Somente quando o pedido envolve fonte, confira `python3 hermes.py source-status` e a
proveniência do runtime. O checkout independente `hermes-agent/` contém a revisão
de `upstream.lock.json` e os patches publicados, registrados em commit local
reproduzível. Ele é ignorado pelo Git pessoal; exporte mudanças para os grupos
de `patches/series.json` por `scripts/export_patch.py`.

Use `python3 hermes.py source` para alinhar uma fonte gerenciada ociosa. Preserve edições,
branches e processos ativos; se necessário, prepare outra fonte com
`make SOURCE=.local/dev-source checkout (no checkout de desenvolvimento da base pública)`. `vm/dev.sh` usa home e ambiente de
desenvolvimento separados. Leia `hermes-agent/AGENTS.md`, as instruções da área
e `docs/desenvolvimento.md` antes de mudar o núcleo. Fontes anteriores ficam
em `.local/source-history/`; não use reset/clean para forçar a troca.

Código comum é alterado na base pública e publicado como release. O repositório
privado adota a versão por PR e seus manifestos controlam o deploy;
ArgoCD atualiza o runtime quando configurado. Upstream, patches e contrato exigem revisão com
revisão da release e do repositório privado. Uma edição local ou build concluído não comprova deploy.
Compare a proveniência e a idade dos logs privados em `.local/diagnostics`.

Use scripts reutilizáveis para verificações. Teste a operação/área alterada e
falhas pendentes; não repita chamadas de IA/API sobre recursos inalterados.
O CI pode executar sua suíte completa. Não inicie backups completos como etapa
automática de instalação de plugin ou alteração desta skill.
