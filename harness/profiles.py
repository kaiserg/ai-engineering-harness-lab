"""Versiones de escenario confiables; el contenido de tools no cambia estos valores."""
from .contracts import ContractError

CI_PREFIXES = ('mcp-devops-ci-v1:', 'mcp-devops-ci-v2:', 'mcp-devops-ci-v3:', 'mcp-devops-ci-v4:')
BUILD_SCENARIOS = ('matching', 'stale', 'failed', 'unavailable')
AGENTIC_CHAT_PROFILE = 'ollama-qwen3-4b-agentic-document-chat-v1'
LIVE_CI_PREFIX = 'devops-ci-live-chat-v1:'
JIRA_CI_PREFIX = 'devops-ci-jira-chat-v1:'
REAL_CI_PREFIXES = (LIVE_CI_PREFIX, JIRA_CI_PREFIX)
CI_CHAT_MODES = {f'ci-{scenario}': f'devops-ci-chat-v1:{scenario}' for scenario in BUILD_SCENARIOS}
CI_CHAT_PROFILES = {profile: mode for mode, profile in CI_CHAT_MODES.items()}
CONTENT_SCENARIOS = ('benign', 'injection')
ATTACKS = ('ignore', 'job', 'shell', 'claim')


def parse_ci_profile(profile):
    if not profile.startswith(CI_PREFIXES):
        return None
    parts = profile.split(':')
    version = parts[0]
    if (len(parts) != (4 if version in ('mcp-devops-ci-v3', 'mcp-devops-ci-v4') else 2)
            or parts[1] not in BUILD_SCENARIOS):
        raise ContractError('CONFIG_CHANGED')
    content, attack = (parts[2], parts[3]) if len(parts) == 4 else (None, 'ignore')
    if len(parts) == 4 and (content not in CONTENT_SCENARIOS or attack not in ATTACKS
                          or content != 'injection' and attack != 'ignore'):
        raise ContractError('CONFIG_CHANGED')
    return version, parts[1], content, attack
