# Patches locais do núcleo

O build baixa a revisão exata de `upstream.lock.json` e aplica os patches
verificados pelo lock. Prefira skills, plugins e a integração própria para
personalizações.

As mudanças ficam separadas por responsabilidade, nesta ordem:

| Patch | Responsabilidade | Verificação |
| --- | --- | --- |
| `0001-runtime-almalinux.patch` | Base AlmaLinux x86-64-v2, bibliotecas e dependências PM do runtime | Build, bootstrap e validação funcional da imagem |
| `0002-ssh-terminal.patch` | Comandos em lotes, sincronização e isolamento de home/perfis no terminal SSH | Seis testes nativos SSH e de sincronização |
| `0003-anthropic-usage.patch` | Preservação dos buckets de tokens e cache Anthropic no adaptador auxiliar | Três testes nativos de Anthropic/MoA |
| `0004-mcp-stdio-executor.patch` | Hook stdio para executores MCP externos e tipo de transporte do catálogo na API da interface | Testes nativos de probes, transporte e catálogo RPC |
| `0005-managed-skills.patch` | Encaminhamento das skills oficiais para a autogerência do runtime separado, preservando instruções nativas | `python3 scripts/check_official_skill_patches.py` reconstrói os patches fixados e compara os arquivos; sem chamadas de modelo |

`series.json` declara os arquivos e testes de cada grupo. O exportador recusa
mudanças sem classificação e arquivos atribuídos a dois grupos. Assim, uma nova
correção exige escolher ou criar um grupo, sem voltar a acumular tudo em
`local.patch`. Grupos sem diferenças para a release base não produzem patch.

Quando uma correção exigir alteração no núcleo, edite o checkout ignorado
`hermes-agent/`, teste com o runner nativo e exporte com
`python3 scripts/export_patch.py --source CAMINHO_DO_CHECKOUT`.
Arquivos novos precisam de `git -C hermes-agent add CAMINHO` antes da exportação.
O exportador compara o checkout com a revisão base e gera cada `.patch`
separadamente. `--test CAMINHO` continua aceito para verificar que o teste já
foi declarado no grupo correspondente. Revise os patches e o lock antes do commit.
O lock registra a ordem, o SHA-256 e os testes de cada patch; sua lista global de
testes é a união dessas listas e continua sendo usada pelo runner da CI.
O build aplica os patches na ordem do lock e verifica seus hashes. O CI executa
os testes indicados antes de publicar; a suíte completa segue sendo manual.

O patch `0005` altera somente instruções Markdown. Cópias personalizadas existentes
em `customizations/<perfil>/skills/` recebem a mesma adaptação, pois podem prevalecer
sobre as skills da imagem. Cada perfil recebe também `hermes-self-management`.
As demais instruções e personalizações são preservadas. A atualização contínua
dessas skills passou a usar a rotina genérica em `runtime/skill_updates.py`,
integrada à preparação da release e ao bootstrap/captura do runtime. Não é mais
necessário executar o publicador pontual a cada atualização.

Para publicar estas instruções em cópias já instaladas, use
`scripts/check_official_skill_patches.py --kubeconfig CAMINHO --publish --base-revision COMMIT_ANTERIOR`.
O script reconstrói a fonte anterior e a nova, mescla alterações independentes com
três versões e recusa conflitos ou resultados ambíguos antes de escrever.
Personalizações mescladas voltam à projeção Git. A publicação usa compare-and-swap
e verifica os hashes. Sem `--publish`, apenas confere.
Ele não instala plugins, reinicia serviços ou chama modelos. Novas sessões carregam
as instruções atualizadas; o fluxo de CI/imagem preserva o patch nas instalações futuras.
