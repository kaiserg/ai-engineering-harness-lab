"""Entrega cifrada por rol; recarga validada sin cambiar UID ni montar todas las claves."""
import base64
import json
import os
from pathlib import Path
import secrets
import ssl
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from .postgres_config import RUNTIME
from .security_lifecycle import atomic_json
from .service_tls import private_write
from .signing_keys import parse_public_keys, public_jwk


def prepare_delivery(directory=RUNTIME):
    for role in ('identity','jira'):
        key = directory / (role + '-delivery-key')
        if not key.exists() and (directory/'live-delivery'/role/'bundle.json').exists():
            raise ValueError('Restore the original delivery key')
        if not key.exists():
            private_write(key, secrets.token_bytes(32))
        target = directory / 'live-delivery' / role
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o755)
    (directory / 'live-delivery').chmod(0o755)


def publish(role, generation, directory=RUNTIME, *, advertised=None):
    prepare_delivery(directory)
    files = {name: base64.b64encode((generation / name).read_bytes()).decode()
             for name in (role+'-cert.pem',role+'-key.pem')}
    if role == 'identity':
        private = (generation / 'issuer/mcp-worker-private.pem').read_bytes()
        previous = json.loads((generation / 'issuer-previous.json').read_text())
        files['issuer-signing.pem'] = base64.b64encode(private).decode()
        files['issuer-previous.json'] = base64.b64encode(json.dumps(previous).encode()).decode()
        advertised = advertised or [public_jwk(private), *previous]
        parse_public_keys(advertised)
    path = directory / 'live-delivery' / role / 'bundle.json'
    sequence = json.loads(path.read_text())['sequence'] + 1 if path.exists() else 1
    data = {'sequence':sequence,'generation':generation.name,'files':files,'advertised':advertised}
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM((directory / (role+'-delivery-key')).read_bytes()).encrypt(
        nonce, json.dumps(data).encode(), role.encode())
    atomic_json(path, {'sequence':sequence, 'nonce':base64.b64encode(nonce).decode(),
        'ciphertext':base64.b64encode(ciphertext).decode()})
    # Solo ciphertext es legible por UID 10001 vía mount; la clave de entrega es un secret separado.
    path.chmod(0o644)
    return sequence


class LiveMaterials:
    def __init__(self, role, directory=RUNTIME, delivery=None):
        self.role, self.directory = role, directory
        self.delivery = delivery or Path('/run/live-delivery/bundle.json')
        self.key = (directory / (role+'-delivery-key')).read_bytes()
        self.current, self.error = None, None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.reload()

    def reload(self):
        envelope = json.loads(self.delivery.read_text())
        data = json.loads(AESGCM(self.key).decrypt(base64.b64decode(envelope['nonce']),
            base64.b64decode(envelope['ciphertext']), self.role.encode()))
        if type(data.get('sequence')) is not int or data['sequence'] != envelope['sequence']:
            raise ValueError('Invalid live sequence')
        old = self.current
        if old and data['sequence'] < old['sequence']:
            raise ValueError('Live rollback rejected')
        if old and data['sequence'] == old['sequence']:
            self.error = None
            return
        files = {name:base64.b64decode(value, validate=True) for name,value in data['files'].items()}
        allowed = {self.role+'-cert.pem',self.role+'-key.pem'}
        if self.role == 'identity':
            allowed |= {'issuer-signing.pem','issuer-previous.json'}
        if set(files) != allowed:
            raise ValueError('Unexpected role material')
        certificate = x509.load_pem_x509_certificate(files[self.role+'-cert.pem'])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise ValueError('Invalid live certificate lifetime')
        # Cadena y SAN se comprueban contra CA fija antes de aceptar la configuración.
        ca_path = self.directory / 'tls-ca.pem'
        if not ca_path.exists():
            ca_path = Path('/run/secrets/tls_ca')
        certificate.verify_directly_issued_by(x509.load_pem_x509_certificate(ca_path.read_bytes()))
        expected = 'identity' if self.role == 'identity' else 'jira-mcp'
        if expected not in certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName):
            raise ValueError('Invalid live certificate SAN')
        destination = self.directory / ('live-' + str(data['sequence']))
        destination.mkdir(mode=0o700, exist_ok=True)
        for name,value in files.items():
            path = destination/name
            fd = os.open(path,os.O_CREAT|os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as handle:
                handle.write(value)
            path.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(destination/(self.role+'-cert.pem')),str(destination/(self.role+'-key.pem')))
        snapshot = {'sequence':data['sequence'],'generation':data['generation'],
            'serial':str(certificate.serial_number),'not_after':certificate.not_valid_after_utc.timestamp(),'context':context,'directory':destination}
        if self.role == 'identity':
            from .signing_keys import public_jwk
            active = public_jwk(files['issuer-signing.pem'])
            keys = data['advertised']
            parse_public_keys(keys)
            if active['kid'] not in {item['kid'] for item in keys}:
                raise ValueError('Signer missing from advertised keys')
            snapshot.update(private=files['issuer-signing.pem'], jwk=active, advertised=keys)
        with self.lock:
            self.current, self.error = snapshot, None

    def snapshot(self):
        with self.lock:
            return self.current

    def start(self):
        def watch():
            while not self.stopping.wait(.2):
                try:
                    self.reload()
                except Exception:
                    # Configuración inválida no sustituye la última validada; nunca imprimir material.
                    self.error = 'LIVE_CONFIGURATION_REJECTED'
        self.thread = threading.Thread(target=watch, daemon=True, name='live-materials')
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if hasattr(self,'thread'):
            self.thread.join(timeout=2)


def run_live(app, live, *, host, port):
    from .metrics import attach_certificate
    attach_certificate(lambda:live.snapshot()['not_after'])
    import uvicorn
    snapshot = live.snapshot()
    config = uvicorn.Config(app,host=host,port=port,access_log=False,
        ssl_keyfile=str(snapshot['directory']/(live.role+'-key.pem')),
        ssl_certfile=str(snapshot['directory']/(live.role+'-cert.pem')))
    config.load()
    # El callback también recibe server_name=None para clientes por IP.
    def choose_context(sock, server_name, initial):
        sock.context = live.snapshot()['context']
    config.ssl.sni_callback = choose_context
    live.start()
    try:
        uvicorn.Server(config).run()
    finally:
        live.stop()
