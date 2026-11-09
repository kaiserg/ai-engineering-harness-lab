"""Regla determinista: el texto del modelo no concede validez a la evidencia."""
from .contracts import ContractError, FinalAnswer


def finalize_ci(run):
    if [r.tool_name for r in run.results] != ['jira_get_issue', 'github_get_pull_request', 'jenkins_get_build']:
        raise ContractError('INCOMPLETE_EVIDENCE')
    jira, github, build = run.results
    link = jira.data.get('pull_request')
    if (link != {'repository': github.data['repository'], 'pull_number': github.data['pull_number']}
            or github.data['repository'] != build.data['repository']
            or (build.data['repository'], build.data['job']) != ('demo/atlas', 'atlas-tests')):
        raise ContractError('EVIDENCE_MISMATCH')
    same_revision = build.data['revision'] == github.data['head_revision']
    successful = build.data['result'] == 'SUCCESS'
    status = 'STALE_BUILD' if not same_revision else 'VERIFIED' if successful else 'BUILD_FAILED'
    assessment = {'status': status, 'same_revision': same_revision, 'build_successful': successful,
                  'pr_revision': github.data['head_revision'], 'build_revision': build.data['revision'],
                  'sources': [r.source_id for r in run.results]}
    explanation = {'VERIFIED': 'El build pasó para la revisión consultada del PR.',
                   'STALE_BUILD': 'El build corresponde a otra revisión; no valida la revisión consultada del PR.',
                   'BUILD_FAILED': 'El build falló para la revisión consultada del PR.'}[status]
    text = (f"{jira.data['issue_key']} [{jira.source_id}]. "
            f"PR {github.data['repository']}#{github.data['pull_number']}, {github.data['head_revision']} [{github.source_id}]. "
            f"Build {build.data['job']}#{build.data['build_number']}, {build.data['result']}, "
            f"{build.data['revision']} [{build.source_id}]. {explanation} "
            'Esta comparación no autoriza un despliegue ni garantiza que el PR no haya cambiado después.')
    return FinalAnswer(text), assessment


def finalize_ci_partial(run):
    if not run.tool_failures:
        return finalize_ci(run)
    if ([r.tool_name for r in run.results] != ['jira_get_issue', 'github_get_pull_request']
            or len(run.tool_failures) != 1):
        raise ContractError('INCOMPLETE_EVIDENCE')
    jira, github = run.results
    failure = run.tool_failures[0]
    if (failure.call.name != 'jenkins_get_build'
            or failure.call.arguments != {'job': 'atlas-tests', 'build_number': 101}
            or failure.code not in ('UPSTREAM_UNAVAILABLE', 'UPSTREAM_TIMEOUT')
            or github.data['repository'] != 'demo/atlas'
            or jira.data.get('pull_request') != {'repository': github.data['repository'],
                                               'pull_number': github.data['pull_number']}):
        raise ContractError('EVIDENCE_MISMATCH')
    assessment = {'status': 'INCOMPLETE', 'same_revision': None, 'build_successful': None,
                  'pr_revision': github.data['head_revision'], 'build_revision': None,
                  'sources': [r.source_id for r in run.results],
                  'missing': [{'tool_name': failure.call.name, 'arguments': dict(failure.call.arguments),
                               'code': failure.code}]}
    text = (f"Informe parcial: {jira.data['issue_key']}, {jira.data['status']} [{jira.source_id}]. "
            f"PR {github.data['repository']}#{github.data['pull_number']}, {github.data['state']}, "
            f"{github.data['head_revision']} [{github.source_id}]. "
            f"Falta evidencia de Jenkins para atlas-tests#101 ({failure.code}). "
            'No podemos confirmar si CI pasó para esa revisión. Este informe no autoriza un despliegue.')
    return FinalAnswer(text), assessment
