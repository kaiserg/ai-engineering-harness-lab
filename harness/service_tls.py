"""CA local del laboratorio: confianza explícita, sin modificar el almacén del sistema."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
import secrets
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

from .postgres_config import RUNTIME
from .service_identity import prepare_keys


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def material_directory(directory=RUNTIME):
    pointer = directory / 'security-current'
    if pointer.is_symlink():
        if not pointer.exists():
            raise ValueError('Broken security generation pointer')
        return pointer.resolve()
    return directory


def prepare_tls(directory=RUNTIME):
    directory = material_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    expected = ['tls-ca.pem', 'tls-ca-key.pem', 'jira-cert.pem', 'jira-key.pem',
                'identity-cert.pem', 'identity-key.pem', 'mcp-client-secret']
    existing = [(directory / name).exists() for name in expected]
    if any(existing) and not all(existing):
        raise ValueError('Incomplete TLS material; restore the original files.')
    now = datetime.now(timezone.utc)
    if not any(existing):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Harness Lab Local CA')])
        ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .sign(ca_key, hashes.SHA256()))
        def save_pair(name, key, cert):
            private_write(directory / name[0], key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            private_write(directory / name[1], cert.public_bytes(serialization.Encoding.PEM))
        save_pair(('tls-ca-key.pem', 'tls-ca.pem'), ca_key, ca)
        for service, hostname in [('jira', 'jira-mcp'), ('identity', 'identity')]:
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca.subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname), x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(ca_key, hashes.SHA256()))
            save_pair((service + '-key.pem', service + '-cert.pem'), key, cert)
        private_write(directory / 'mcp-client-secret', secrets.token_hex(32).encode())
    ca = x509.load_pem_x509_certificate((directory / 'tls-ca.pem').read_bytes())
    ca.verify_directly_issued_by(ca)
    ca_key = serialization.load_pem_private_key((directory / 'tls-ca-key.pem').read_bytes(), password=None)
    if (ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            != ca_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            or not ca.not_valid_before_utc <= now < ca.not_valid_after_utc):
        raise ValueError('CA expired or mismatched; renew explicitly.')
    for service in ('jira', 'identity'):
        cert = x509.load_pem_x509_certificate((directory / (service + '-cert.pem')).read_bytes())
        cert.verify_directly_issued_by(ca)
        key = serialization.load_pem_private_key((directory / (service + '-key.pem')).read_bytes(), password=None)
        if (cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc):
            raise ValueError('TLS certificate expired or mismatched; renew explicitly.')
    prepare_keys(directory / 'issuer')
    for name in expected:
        (directory / name).chmod(0o600)


def trust_context(directory=RUNTIME):
    directory = material_directory(directory)
    context = ssl.create_default_context(cafile=str(directory / 'tls-ca.pem'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
