"""Project native customizations to Git-safe files, preserving private values on the PVC."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse

def yaml_module():
    # The terminal's controller uses the JSON projection and the private socket.
    # Native YAML parsing belongs to the core, never an extra host dependency.
    try:
        import hermes_yaml
        return hermes_yaml
    except ImportError:
        import yaml
        return yaml

PRIVATE = {'.git', '.local', '.env', '.venv', 'venv', 'node_modules', '__pycache__',
           '.cache', 'logs', 'cache', 'sessions', 'state', 'data', 'backups', '.pytest_cache'}
SECRET_KEY = re.compile(r'(?:^|[_-])(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|authorization|cookie)(?:$|[_-])', re.I)
CREDENTIAL = re.compile(r'(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})|\bBearer\s+[A-Za-z0-9._~-]{16,})')
SOURCE_SUFFIXES = {'.md', '.py', '.json', '.yaml', '.yml', '.toml', '.txt', '.js', '.ts', '.tsx', '.jsx', '.sh', '.bash', '.html', '.css', '.svg', '.sql', '.jinja', '.j2', '.ini', '.cfg', '.conf', '.lock', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.woff', '.woff2', '.go', '.rs', '.c', '.cpp', '.h', '.hpp', '.proto', '.lua', '.rb', '.ex', '.exs', '.svelte', '.vue', '.mjs', '.cjs', '.xml', '.csv', '.example'}
MAX_FILE = 8 * 1024 * 1024


def dump(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path = Path(path)
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Symlink in managed destination')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def profiles(home):
    result = {'default': home}
    directory = home / 'profiles'
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.iterdir()):
            if path.is_dir() and not path.is_symlink() and not path.name.startswith('.') and (path / 'config.yaml').is_file():
                if path.name == 'default' or not re.fullmatch(r'[A-Za-z0-9_-]+', path.name):
                    raise ValueError('Ambiguous profile name')
                result[path.name] = path
    return result


def environment_secrets(home):
    values = set()
    for path in [root / '.env' for root in profiles(home).values()]:
        if path.is_file() and not path.is_symlink():
            for line in path.read_text().splitlines():
                match = re.match(r'\s*(?:export\s+)?([^=#\s]+)\s*=\s*(.*?)\s*$', line)
                if match and SECRET_KEY.search(match[1]):
                    value = match[2].strip('\"\'')
                    if len(value) >= 8:
                        values.add(value.encode())
    return values


def check_content(data, secrets=()):
    if len(data) > MAX_FILE or any(secret in data for secret in secrets):
        raise ValueError('Sensitive or oversized source; move credentials to the private environment')
    text = data.decode('utf-8', errors='ignore')
    if CREDENTIAL.search(text):
        raise ValueError('Credential in versioned content')


SOURCE_REFERENCE = re.compile(r'\$\{HERMES_SECRET:([a-f0-9]{64})\}')

def sanitize_source(data, location, vault, secrets):
    if not any(value in data for value in secrets) and not CREDENTIAL.search(data.decode('utf-8',errors='ignore')):
        check_content(data)
        return data
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:raise ValueError('Credential in a binary asset; keep that asset in private storage') from None
    def replace(value):
        # Stable across scans, independent of which other credentials were
        # discovered first. A changed literal receives a different reference.
        key=digest((location+'/source-secret/'+digest(value.encode())).encode());vault[key]=value
        return '${HERMES_SECRET:'+key+'}'
    if '-----BEGIN ' in text and 'PRIVATE KEY-----' in text:
        # A header-only replacement would publish the key body. Keep the whole
        # original asset private and render it only on the native filesystem.
        return replace(text).encode()
    for value in sorted(secrets,key=lambda value:(-len(value),value)):
        literal=value.decode()
        while literal in text:text=text.replace(literal,replace(literal),1)
    text=CREDENTIAL.sub(lambda match:replace(match.group()),text)
    encoded=text.encode();check_content(encoded)
    return encoded


def resolve_source(data, vault):
    if b'${HERMES_SECRET:' not in data:return data
    def replace(match):
        if match[1] not in vault:raise ValueError('Private source credential missing')
        return vault[match[1]]
    return SOURCE_REFERENCE.sub(replace,data.decode()).encode()


def sensitive(value, key='', previous=''):
    if isinstance(value,str) and re.fullmatch(r'\$\{[A-Z_][A-Z0-9_]*\}',value):return False
    if SECRET_KEY.search(key) and not key.endswith(('_env', '_env_var', '_path', '_file')):
        return value not in (None, '', False)
    if isinstance(value, str):
        if re.fullmatch(r'\$\{[A-Z_][A-Z0-9_]*\}', value):return False
        if CREDENTIAL.search(value) or SECRET_KEY.search(previous.lstrip('-')):
            return True
        if '://' in value:
            try:
                url = urllib.parse.urlsplit(value)
                if url.password or url.username and url.username != 'git' or any(SECRET_KEY.search(k) for k, _ in urllib.parse.parse_qsl(url.query)):
                    return True
            except ValueError:
                return True
        if value.startswith('--') and '=' in value and SECRET_KEY.search(value.split('=', 1)[0]):
            return True
    return False


def sanitize(value, location, vault, secrets, key='', previous=''):
    if sensitive(value, key, previous) or isinstance(value, str) and any(s.decode() in value for s in secrets):
        identity = digest(location.encode())
        vault[identity] = value
        return {'$hermes_secret': identity}
    if isinstance(value, dict):
        return {k: sanitize(v, location + '/' + k, vault, secrets, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, location + '/' + str(i), vault, secrets, previous=str(value[i-1]) if i else '') for i, v in enumerate(value)]
    return value


def resolve(value, vault):
    if isinstance(value, dict):
        if set(value) == {'$hermes_secret'}:
            if value['$hermes_secret'] not in vault:
                raise ValueError('Private value missing; restore the private state or configure its credentials')
            return vault[value['$hermes_secret']]
        return {k: resolve(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, vault) for v in value]
    return value


def source_files(directory):
    if not directory.is_dir():
        return
    for current, folders, files in os.walk(directory, followlinks=False):
        folders[:] = sorted(f for f in folders if f not in PRIVATE and not f.startswith('.env') and not (Path(current) / f).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if (name.startswith('.env') and name!='.env.example') or name.startswith('id_ed25519') or name in {'auth.json', 'secrets.yaml', '.install-metadata.json'} or path.is_symlink():
                continue
            if path.suffix.lower() in SOURCE_SUFFIXES or name in {'LICENSE', 'Dockerfile', 'Makefile'}:
                yield path


def source_links(root):
    """Preserve native skill aliases without following them into private directories."""
    links={}
    for area in ['skills','plugins']:
        directory=root/area
        if not directory.is_dir() or directory.is_symlink():continue
        for current,folders,files in os.walk(directory,followlinks=False):
            folders[:]=[f for f in folders if f not in PRIVATE]
            for name in folders+files:
                path=Path(current)/name
                if not path.is_symlink():continue
                resolved=path.resolve()
                if not any(resolved.is_relative_to((root/a).resolve()) for a in ['skills','plugins']):
                    raise ValueError('Source alias points outside versioned skills/plugins: '+path.relative_to(root).as_posix())
                links[path.relative_to(root).as_posix()]=resolved.relative_to(root).as_posix()
    return links


def restore_links(root, links):
    for name,destination in links.items():
        for value in [name,destination]:
            parts=PurePosixPath(value).parts
            if not parts or parts[0] not in {'skills','plugins'} or '..' in parts or any(p in PRIVATE for p in parts):
                raise ValueError('Invalid source alias')
        path=root/name;target=root/destination
        if not target.exists():raise ValueError('Missing source alias destination: '+destination)
        if any(p.is_symlink() for p in path.parents):raise ValueError('Source alias parent is a symlink')
        if path.is_symlink():
            if path.resolve()!=target.resolve():raise ValueError('Concurrent source alias change')
            continue
        if path.exists():raise ValueError('Source alias would replace existing code')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.symlink_to(os.path.relpath(target,path.parent),target_is_directory=target.is_dir())


def python_installation(home, source):
    key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    facts = home / 'installs' / key / 'facts.json'
    if not facts.is_file():return None
    record = json.loads(facts.read_text()).get('packages',{}).get('venv',{})
    lock = Path(record.get('resolved_lock','/missing-versioned-python-lock'))
    if not lock.is_file():raise ValueError('Native PM dependency lock is missing')
    if not lock.resolve().is_relative_to((home/'installs').resolve()):
        raise ValueError('Native PM lock is outside its install store')
    from pm.workspace import enabled_member_dirs, _member_key
    members={}
    for path in enabled_member_dirs():
        if not path.resolve().is_relative_to(home.resolve()):
            raise ValueError('PM plugin dependency is outside the selected home')
        members[path.resolve().relative_to(home.resolve()).as_posix()]=_member_key(path)
    text=lock.read_text().replace(str(home.resolve()),'${HERMES_HOME}').replace(str(source.resolve()),'${HERMES_SOURCE}')
    return {'schema':1,'extras':record.get('extras',[]),'lock':text,'members':members}


def portable_python_lock(home,source,record):
    from pm.workspace import _member_key
    text=record['lock']
    for relative,old_key in record.get('members',{}).items():
        parts=PurePosixPath(relative).parts
        if not parts or '..' in parts or PurePosixPath(relative).is_absolute():raise ValueError('Invalid PM member path')
        path=home/relative
        if not path.resolve().is_relative_to(home.resolve()):raise ValueError('PM member outside home')
        text=text.replace(old_key,_member_key(path))
    return text.replace('${HERMES_HOME}',str(home.resolve())).replace('${HERMES_SOURCE}',str(source.resolve()))


def restore_python_installation(home, record, source=Path('/opt/hermes')):
    """Use PM's own frozen workspace builder and facts writer; never mutate a live venv."""
    import shutil
    import uuid
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token=set_hermes_home_override(str(home))
    try:
        from pm.environments import install_state_dir, runtime_facts_path
        from pm.environment import managed_environment
        from pm.workspace import enabled_member_dirs, lock_and_sync
        from pm.packages import Venv
        from pm.native_build import source_build_environment
        from pm.lock import Facts
        from pm.features import write_features
        from pm.filesystem import lock_fd
        install=install_state_dir(source)
        if not install.resolve().is_relative_to((home/'installs').resolve()):
            raise ValueError('Native PM resolves this home to another installation; refusing to alter it')
        install.mkdir(mode=0o700,parents=True,exist_ok=True)
        with (install/'.prepare.lock').open('a+b') as lease:
            lock_fd(lease.fileno(),wait=True)
            generation=install/'environments'/uuid.uuid4().hex
            seed=home/'state/versioning/python-seed.lock'
            atomic(seed,portable_python_lock(home,source,record).encode())
            candidate=generation/'venv'
            try:
                generation.mkdir(parents=True)
                environment=managed_environment(candidate,env=source_build_environment(source),explicit=True)
                environment.create()
                members=enabled_member_dirs()
                lock_and_sync(members,record['extras'],root=generation/'workspace',source=source,seed_lock=seed,environment=environment,frozen=True)
                environment.check()
                (generation/'.lease-managed').touch()
                stamp=Venv().expected_stamp(record['extras'],plugin_dirs=members)
                Facts(runtime_facts_path(source)).record_state('venv',stamp,record['extras'],environment=candidate,resolved_lock=generation/'workspace/uv.lock')
                write_features(record['extras'],home)
            except BaseException:
                shutil.rmtree(generation,ignore_errors=True)
                raise
            finally:
                seed.unlink(missing_ok=True)
    finally:
        reset_hermes_home_override(token)


def mcp_installations(home, root, profile, configuration):
    policy_file = Path('/etc/hermes-policy/instance.json')
    if not policy_file.is_file():return None
    from mcp_update import execute
    definitions = configuration.get('mcp_servers', {})
    signature = digest(dump(definitions))
    cache = home / 'state/versioning/mcp' / (profile + '.json')
    previous = json.loads(cache.read_text()) if cache.is_file() else {}
    if previous.get('signature') == signature and previous.get('complete') and time.time()-previous.get('checked_at',0)<900:
        return previous['export']
    report=execute('list',['--all'],definitions,profile,home,json.loads(policy_file.read_text()),policy_file.parent)
    records, locks, complete = {}, {}, report['ok']
    for row in report['results']:
        recipe=row.get('installation_recipe')
        entry={k:row[k] for k in ['status','active_versions','provider_managed','package_policy'] if k in row}
        if recipe:
            recipe=json.loads(json.dumps(recipe))
            dependency={k:recipe['recipe'].pop(k) for k in ['manifest','lock','requirements'] if k in recipe['recipe']}
            identity=digest(dump(dependency));locks[identity]=dependency
            recipe['recipe']['dependency_lock']=identity;entry['installation_recipe']=recipe
        elif row.get('active_versions'):
            complete=False
        records[row['name']]=entry
    exported={'schema':1,'servers':records,'locks':locks,'complete':complete}
    atomic(cache,dump({'signature':signature,'checked_at':time.time(),'complete':complete,'export':exported}))
    return exported


def restore_mcp_installations(home, root, profile, record):
    from mcp_update import execute
    policy=Path('/etc/hermes-policy/instance.json')
    if not policy.is_file():raise ValueError('MCP deployment policy is required to restore package locks')
    definitions=yaml_module().safe_load((root/'config.yaml').read_text()).get('mcp_servers',{})
    recipes={}
    for name,row in record['servers'].items():
        if row.get('installation_recipe'):
            recipe=json.loads(json.dumps(row['installation_recipe']))
            key=recipe['recipe'].pop('dependency_lock');recipe['recipe'].update(record['locks'][key]);recipes[name]=recipe
    if recipes:
        result=execute('restore',list(recipes),definitions,profile,home,json.loads(policy.read_text()),policy.parent,recipes=recipes)
        if not result['ok']:raise ValueError('Exact MCP restoration failed; previous generations retained')
    (home/'state/versioning/mcp'/(profile+'.json')).unlink(missing_ok=True)


def capture(home, source=Path('/opt/hermes'), known=None, bundle=Path('/opt/hermes-personal')):
    home = Path(home).resolve()
    bundle = Path(bundle)
    # Real runtimes refresh managed skills before projection; offline fixtures
    # without an image keep their existing capture semantics.
    if (source/'source-provenance.json').is_file() and (bundle/'skills').is_dir():
        from skill_updates import installed_update
        installed_update(home, source, bundle)
    private = home / 'state/versioning/secrets.json'
    if not private.exists():atomic(private, dump({}))
    vault = json.loads(private.read_text()) if private.is_file() else {}
    secrets = environment_secrets(home)
    files, recipes, skipped, distributed = {}, {}, [], []
    def add(name, data, mode=0o600):
        check_content(data, secrets)
        checksum=digest(data)
        files[name] = {'sha256':checksum,'mode':mode}
        if known is None or known.get(name)!=checksum:files[name]['data']=base64.b64encode(data).decode()
    for profile, root in profiles(home).items():
        prefix = f'customizations/{profile}/'
        if not (root / 'config.yaml').is_file():continue
        configuration = yaml_module().safe_load((root / 'config.yaml').read_text()) if (root / 'config.yaml').is_file() else {}
        # Export original commands rather than a transport wrapper left by older deployments.
        try:
            from configuration import restore_mcp
            previous = root / 'state/hermes-ssh-mcp.json'
            configuration = restore_mcp(configuration, json.loads(previous.read_text()) if previous.is_file() else {})
        except ImportError:
            pass
        add(prefix + 'config.json', dump(sanitize(configuration, profile + '/config', vault, secrets)))
        secrets.update(value.encode() for value in vault.values() if isinstance(value,str) and len(value)>=8)
        for instruction in ['SOUL.md','AGENTS.md','USER.md','TOOLS.md']:
            path=root/instruction
            if path.is_file() and not path.is_symlink():add(prefix+'persona/'+instruction,sanitize_source(path.read_bytes(),profile+'/persona/'+instruction,vault,secrets))
        features = root / 'enabled-features.json'
        if features.is_file():
            add(prefix + 'features.json', dump(json.loads(features.read_text())))
        python = python_installation(home, source) if profile == 'default' else None
        if python is not None:add(prefix + 'python-lock.json', dump(sanitize(python, profile + '/python-lock', vault, secrets)))
        packages = mcp_installations(home, root, profile, configuration)
        if packages is not None:add(prefix + 'mcp-lock.json', dump(sanitize(packages, profile + '/mcp-lock', vault, secrets)))
        metadata = root / 'plugins/.install-metadata.json'
        record = json.loads(metadata.read_text()) if metadata.is_file() else {}
        recipe = {'schema': 1, 'plugins': sanitize(record, profile + '/plugins', vault, secrets), 'links':source_links(root), 'source_mode': 'snapshot-with-native-install-provenance'}
        for area in ['skills', 'plugins']:
            for path in source_files(root / area):
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                # Unmodified built-ins are reconstructed from the fixed upstream, not duplicated.
                baseline = source / relative
                if area == 'skills' and baseline.is_file() and baseline.read_bytes() == data:
                    skipped.append(profile + '/' + relative)
                    continue
                distribution = bundle/relative
                if area == 'skills' and distribution.is_file() and distribution.read_bytes() == data:
                    distributed.append(profile + '/' + relative)
                    continue
                try:data=sanitize_source(data,profile+'/'+relative,vault,secrets)
                except ValueError as error:raise ValueError('Source needs review: '+profile+'/'+relative) from error
                add(prefix + relative, data, 0o700 if path.stat().st_mode & 0o111 else 0o600)
        recipe['upstream_skills'] = sorted(p.removeprefix(profile + '/') for p in skipped if p.startswith(profile + '/'))
        recipe['distribution_skills'] = sorted(p.removeprefix(profile + '/') for p in distributed if p.startswith(profile + '/'))
        provenance = home / 'state/hermes-provenance.json'
        if provenance.is_file():
            info = json.loads(provenance.read_text())
            recipe['upstream_revision'] = info.get('upstream_revision', info.get('upstream_commit', info.get('revision', info.get('code_sha'))))
        add(prefix + 'installations.json', dump(recipe))
        recipes[profile] = {'skills': sum(n.startswith(prefix+'skills/') and n.endswith('SKILL.md') for n in files), 'source_files': sum(n.startswith(prefix) for n in files), 'plugins': len(record)}
    if vault and (not private.is_file() or private.read_bytes() != dump(vault)):
        atomic(private, dump(vault))
    return {'schema': 1, 'files': files, 'profiles': recipes}


def target(home, name):
    parts = PurePosixPath(name).parts
    if len(parts) < 3 or parts[0] != 'customizations' or '..' in parts:
        raise ValueError('Invalid customization path')
    roots = profiles(home)
    profile = parts[1]
    if profile not in roots:
        # A new profile is a native config directory with its own environment.
        if not re.fullmatch(r'[A-Za-z0-9_-]+', profile) or profile == 'default':
            raise ValueError('Invalid profile')
        root = home / 'profiles' / profile
    else:
        root = roots[profile]
    relative = Path(*parts[2:])
    if len(relative.parts)==2 and relative.parts[0]=='persona' and relative.name in {'SOUL.md','AGENTS.md','USER.md','TOOLS.md'}:
        return root/relative.name,'source'
    if relative.as_posix() == 'config.json':
        return root / 'config.yaml', 'config'
    if relative.as_posix() == 'features.json':
        return root / 'enabled-features.json', 'features'
    if relative.as_posix() == 'python-lock.json':
        return root / 'state/versioning/desired-python.json', 'python'
    if relative.as_posix() == 'mcp-lock.json':
        return root / 'state/versioning/desired-mcp.json', 'mcp'
    if relative.as_posix() == 'installations.json':
        return root / 'plugins/.install-metadata.json', 'installations'
    if relative.parts[0] not in {'skills', 'plugins'} or any(p in PRIVATE or p.startswith('.env') and p!='.env.example' for p in relative.parts):
        raise ValueError('Invalid customization source path')
    return root / relative, 'source'


def apply(home, changes, source=Path('/opt/hermes'), bundle=Path('/opt/hermes-personal')):
    """CAS against the exported representation; back up originals before every write/removal."""
    home = Path(home).resolve()
    current = capture(home, source, known={name:change.get('expected') for name,change in changes.items()},bundle=bundle)['files']
    vault_path = home / 'state/versioning/secrets.json'
    vault = json.loads(vault_path.read_text()) if vault_path.is_file() else {}
    secrets=environment_secrets(home)
    prepared = []
    aliases = []
    for name, change in changes.items():
        actual = current.get(name, {}).get('sha256')
        if actual != change.get('expected'):
            raise ValueError('Concurrent native edit: ' + name)
        path, kind = target(home, name)
        data = base64.b64decode(change['data'], validate=True) if change.get('data') is not None else None
        managed_restore = False
        parts = PurePosixPath(name).parts
        relative = '/'.join(parts[2:])
        if data is None and kind == 'source' and relative.startswith('skills/'):
            recipe_name = '/'.join(parts[:2])+'/installations.json'
            recipe_item = changes.get(recipe_name,current.get(recipe_name,{}))
            recipe = json.loads(base64.b64decode(recipe_item['data'])) if recipe_item.get('data') else {}
            for field, directory in [('upstream_skills',Path(source)),('distribution_skills',Path(bundle))]:
                if relative in recipe.get(field,[]):
                    template = directory/relative
                    if not template.is_file() or any(p.is_symlink() for p in [template,*template.parents]):
                        raise ValueError('Managed skill template missing during projection restoration')
                    data = template.read_bytes()
                    managed_mode = 0o700 if template.stat().st_mode & 0o111 else 0o600
                    managed_restore = True
                    break
        if data is not None:
            if not managed_restore:check_content(data,secrets)
            if kind == 'source' and not managed_restore:data=resolve_source(data,vault)
            if kind in {'config', 'features', 'installations', 'mcp', 'python'}:
                value = resolve(json.loads(data), vault)
                if kind == 'config':
                    if not isinstance(value, dict):raise ValueError('Configuration must be an object')
                    data = yaml_module().safe_dump(value, allow_unicode=True, sort_keys=False).encode()
                elif kind == 'installations':
                    aliases.append((path.parent.parent,value.get('links',{})))
                    data = dump(value['plugins'])
                else:
                    data = dump(value)
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Managed path is a symlink')
        if data is None and kind != 'source':
            raise ValueError('Remove profiles/installations through their native managers')
        prepared.append((name, path, data, managed_mode if managed_restore else change.get('mode', 0o600), kind))
    backup = home / 'state/versioning/history'
    written=[]
    try:
        for name, path, data, mode, kind in prepared:
            old = path.read_bytes() if path.is_file() else None
            old_mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o600
            if old is not None:
                saved = backup / digest(name.encode()) / digest(old)
                if not saved.exists():atomic(saved, old)
            if data is None:path.unlink(missing_ok=True)
            else:atomic(path, data, mode)
            written.append((path,old,old_mode,data))
        for name, path, data, mode, kind in prepared:
            if kind == 'python' and data is not None:
                restore_python_installation(home,json.loads(data))
            if kind == 'mcp' and data is not None:
                profile=PurePosixPath(name).parts[1]
                restore_mcp_installations(home, profiles(home)[profile], profile, json.loads(data))
        for root,links in aliases:restore_links(root,links)
    except BaseException:
        for path,old,old_mode,written_data in reversed(written):
            # Never roll back another editor's work. Every preimage also remains
            # in history; native PM/MCP retain their verified generations.
            if path.is_symlink():continue
            present=path.read_bytes() if path.is_file() else None
            if present!=written_data:continue
            if old is None:path.unlink(missing_ok=True)
            else:atomic(path,old,old_mode)
        raise
    from prepare_state import separate_env_files
    separate_env_files(home)
    plugin_profiles={PurePosixPath(name).parts[1] for name,_,_,_,kind in prepared if kind=='installations' or '/plugins/' in name}
    reloaded={}
    try:
        from gateway.control_socket import reload_gateway_plugins
        for profile in sorted(plugin_profiles):
            reply=reload_gateway_plugins(home,profile_home=profiles(home)[profile])
            reloaded[profile]=bool(reply and reply.get('reloaded'))
    except ImportError:
        pass  # Offline restore has no running native gateway to notify.
    return {'applied':len(prepared),'plugins_reloaded':reloaded,
            'restart_required':any(kind in {'features','python'} for _,_,_,_,kind in prepared) or any(not reloaded.get(profile) for profile in plugin_profiles)}


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['capture','apply'])
    p.add_argument('--home', type=Path, default=Path(os.environ.get('HERMES_HOME', '/opt/data')))
    p.add_argument('--changes',type=Path)
    a=p.parse_args()
    result=capture(a.home) if a.operation=='capture' else apply(a.home,json.loads(a.changes.read_text()))
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
