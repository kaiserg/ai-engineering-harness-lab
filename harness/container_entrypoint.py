"""Bootstrap local del contenedor: archivo privado y proceso principal sin root."""
import os
from pathlib import Path
import re
import sys


def main():
    try:
        role = os.environ.get('HARNESS_CONTAINER_ROLE', 'api')
        required = {'api': ['postgres_app_password'],
                    'worker': ['postgres_app_password', 'tls_ca', 'mcp_client_secret'],
                    'jira': ['tls_ca', 'jira_cert', 'jira_key', 'jira_delivery_key'],
                    'identity': ['identity_cert', 'identity_key', 'issuer_signing', 'issuer_previous', 'mcp_client_secret', 'identity_delivery_key']}[role]
        for name in required:
            source = Path('/run/secrets') / name
            destination = {'postgres_app_password': 'postgres-app-password',
                           'tls_ca': 'tls-ca.pem', 'mcp_client_secret': 'mcp-client-secret',
                           'jira_cert': 'jira-cert.pem', 'jira_key': 'jira-key.pem',
                           'identity_cert': 'identity-cert.pem', 'identity_key': 'identity-key.pem',
                           'issuer_signing': 'issuer-signing.pem', 'issuer_previous': 'issuer-previous.json',
                           'identity_delivery_key':'identity-delivery-key', 'jira_delivery_key':'jira-delivery-key'}[name]
            data = source.read_bytes()
            target = Path('/app/data/runtime') / destination
            descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
            os.chmod(target, 0o600)
            os.chown(target, 10001, 10001)
        if role in ('api', 'worker'):
            secret = Path('/app/data/runtime/postgres-app-password').read_text().strip()
            if not re.fullmatch('[a-f0-9]{64}', secret):
                raise ValueError('invalid secret')
            del secret
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
        os.umask(0o077)
        if not sys.argv[1:]:
            raise ValueError('missing service')
        os.execvp(sys.argv[1], sys.argv[1:])
    except (OSError, ValueError, KeyError):
        print('CONTAINER_CONFIGURATION_ERROR', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
