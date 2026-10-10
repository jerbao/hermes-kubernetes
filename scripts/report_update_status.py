"""Keep one actionable GitHub issue for blocked upstream updates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

MARKER = '<!-- hermes-upstream-update-alert -->'


class GitHub:
    def __call__(self, method, endpoint, data=None):
        command = ['gh', 'api', '--method', method, endpoint]
        with tempfile.TemporaryDirectory(prefix='hermes-update-alert-') as temporary:
            if data is not None:
                payload = Path(temporary)/'request.json'
                payload.write_text(json.dumps(data))
                command += ['--input', str(payload)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # API error bodies can contain request data; keep the failure concise.
            raise RuntimeError('GitHub alert request failed: '+method+' '+endpoint)
        return json.loads(result.stdout) if result.stdout.strip() else None


def open_alert(api, repo, marker=MARKER):
    page = 1
    while True:
        issues = api('GET', f'repos/{repo}/issues?state=open&per_page=100&page={page}')
        for issue in issues:
            if 'pull_request' not in issue and marker in (issue.get('body') or ''):
                return issue
        if len(issues) < 100:
            return None
        page += 1


def report(api, repo, run_id, attempt, status, release=None, users=(), incident='upstream'):
    marker = MARKER if incident == 'upstream' else '<!-- hermes-image-update-alert -->'
    issue = open_alert(api, repo, marker)
    url = f'https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}'
    if status == 'success':
        if issue:
            api('POST', f'repos/{repo}/issues/{issue["number"]}/comments', {
                'body':f'O workflow voltou a concluir com sucesso: [execução]({url}). '
                       'Isso não significa que a atualização foi implantada em produção.'})
            api('PATCH', f'repos/{repo}/issues/{issue["number"]}', {'state':'closed'})
        return {'status':'recovered' if issue else 'healthy'}
    jobs = api('GET', f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    failed = [step['name'] for job in jobs.get('jobs', [])
              for step in job.get('steps', []) if step.get('conclusion') in {'failure','cancelled'}]
    release = release or {}
    patches = [p['path'] for p in release.get('patches', []) if p.get('status') == 'conflict']
    skills = release.get('skill_conflicts',[])
    summary = {'release':release.get('tag'), 'status':status, 'steps':failed, 'patches':patches,'skills':skills}
    signature = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    identity = f'<!-- failure-signature:{signature} -->'
    mentions = ' '.join('@'+user for user in users)
    body = '\n'.join([
        marker, identity, mentions, '',
        '**A avaliação da atualização do Hermes está bloqueada e precisa de atenção.**', '',
        f'- Release candidata: `{release.get("tag") or "não identificada"}`.',
        '- Etapa com falha: '+(', '.join(failed) or 'consulte a execução')+'.',
        '- Patches em conflito: '+(', '.join(patches) or 'nenhum identificado no relatório')+'.',
        '- Skills em conflito: '+(', '.join(skills) or 'nenhuma identificada no relatório')+'.',
        f'- Última execução: [abrir logs e artifacts]({url}).', '',
        'Corrija a causa e execute o workflow novamente. O relatório de skills, quando '
        'disponível, está no artifact `hermes-skill-review-'+str(run_id)+'`. '
        'Consulte a execução para confirmar as etapas de publicação/deploy.', '',
        'Falhas idênticas atualizam esta issue sem criar outra ou enviar comentários '
        'repetidos. Uma causa diferente gera novo comentário; a recuperação fecha a issue.',
    ])
    if issue:
        endpoint = f'repos/{repo}/issues/{issue["number"]}'
        if identity not in (issue.get('body') or ''):
            api('POST', endpoint+'/comments', {'body':mentions+'\n\nA causa do bloqueio mudou.\n\n'+body})
        api('PATCH', endpoint, {'body':body})
        return {'status':'updated', 'issue':issue['number']}
    created = api('POST', f'repos/{repo}/issues', {
        'title':'[Hermes] Atualização upstream bloqueada' if incident == 'upstream' else '[Hermes] Publicação bloqueada', 'body':body,
        **({'assignees':list(users)} if users else {}),
    })
    return {'status':'opened', 'issue':created['number']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', required=True, choices=['success','failure','cancelled'])
    parser.add_argument('--report', type=Path, default=Path('.build/upstream-release.json'))
    parser.add_argument('--skill-report',type=Path,default=Path('.build/skill-update.json'))
    parser.add_argument('--incident',choices=['upstream','image'],default='upstream')
    args = parser.parse_args()
    release = {}
    if args.report.is_file():
        try: release = json.loads(args.report.read_text())
        except (OSError, ValueError): pass  # Missing/broken reports must still produce an alert.
    if args.skill_report.is_file():
        try: release['skill_conflicts'] = json.loads(args.skill_report.read_text()).get('conflicts',[])
        except (OSError, ValueError): pass
    users = [u.strip().lstrip('@') for u in os.environ.get('UPSTREAM_ALERT_USERS','').split(',') if u.strip()]
    result = report(GitHub(), os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_RUN_ID'],
                    os.environ.get('GITHUB_RUN_ATTEMPT','1'), args.status, release, users, args.incident)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
