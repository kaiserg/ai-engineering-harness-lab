"""Catálogo fijo de la primera herramienta MCP; no autorizar por descubrimiento."""
JIRA_INPUT_SCHEMA = {
    'type': 'object',
    'properties': {'issue_key': {'type': 'string', 'pattern': r'^[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,8}$'}},
    'required': ['issue_key'],
    'additionalProperties': False,
}


GITHUB_INPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'repository': {'type': 'string', 'pattern': r'^[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'},
        'pull_number': {'type': 'integer', 'minimum': 1, 'maximum': 1_000_000},
    },
    'required': ['repository', 'pull_number'],
    'additionalProperties': False,
}


JENKINS_INPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'job': {'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
        'build_number': {'type': 'integer', 'minimum': 1, 'maximum': 1_000_000},
    },
    'required': ['job', 'build_number'],
    'additionalProperties': False,
}
