"""Controlador local por fases; ninguna llamada al daemon Docker durante el cambio."""
import json
import time
import urllib.request
from .postgres_config import RUNTIME
from .security_lifecycle import (ACTIONS, activate, atomic_json, ensure_generation,
    operation_lock, prepare_change, validate, status, sync_directory)
from .service_tls import trust_context
from .signing_keys import public_jwk
from .live_materials import publish


def health(role, directory=RUNTIME):
    port = 8011 if role == 'identity' else 8010
    with urllib.request.urlopen('https://127.0.0.1:'+str(port)+'/health',
            context=trust_context(directory),timeout=2) as response:
        return json.load(response)


def wait_health(role, predicate, directory=RUNTIME, timeout=25):
    until = time.monotonic()+timeout
    while time.monotonic()<until:
        try:
            state = health(role,directory)
            if not state.get('live_error') and predicate(state):
                return state
        except Exception:
            pass
        time.sleep(.2)
    raise ValueError('Live service acknowledgement timed out')


def perform_live(action, directory=RUNTIME, *, publisher=publish, waiter=wait_health):
    journal = directory/'security-live-operation.json'
    with operation_lock(directory):
        base = ensure_generation(directory)
        if (directory/'security-operation.json').exists():
            raise ValueError('Coordinated maintenance is pending')
        if action == 'recover':
            if not journal.exists():
                raise ValueError('No pending live operation')
            operation = json.loads(journal.read_text())
        else:
            if journal.exists():
                raise ValueError('Recover the pending live operation first')
            if action not in ACTIONS:
                raise ValueError('Invalid live action')
            # Preparar no cambia el puntero; una ventana previa no finalizada bloquea otra firma.
            target = prepare_change(action,directory)
            operation = {'action':action,'base':base.name,'target':target.name,'phase':'prepared'}
            atomic_json(journal,operation)
        if (operation.get('action') not in ACTIONS or operation.get('phase') not in
                ('prepared','announcing','switching','switched','delivered')):
            raise ValueError('Invalid live operation journal')
        for name in ('base','target'):
            value=operation.get(name,'')
            if len(value)!=32 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('Invalid generation id')
        source=directory/'security-generations'/operation['base']
        target=directory/'security-generations'/operation['target']
        validate(target)
        action=operation['action']
        def phase(value):
            operation['phase']=value
            atomic_json(journal,operation)
        active=public_jwk((target/'issuer/mcp-worker-private.pem').read_bytes())
        if action=='rotate-signing-key':
            if operation['phase'] in ('prepared','announcing'):
                # Publicar nueva pública antes de usar su privada. Persistir intención antes del efecto.
                phase('announcing')
                old=public_jwk((source/'issuer/mcp-worker-private.pem').read_bytes())
                sequence=publisher('identity',source,directory,advertised=[old,active])
                waiter('identity',lambda state:state.get('sequence',0)>=sequence,directory)
                waiter('jira',lambda state:active['kid'] in state.get('known_kids',[]) and state.get('jwks_fresh'),directory)
                phase('switching')
            if operation['phase']=='switching':
                # Fijar una sola vez: recuperar nunca revive una clave cuyo retiro ya ocurrió.
                if 'switch_deadline' not in operation:
                    operation['switch_deadline']=int(time.time())+90
                    atomic_json(journal,operation)
                previous=json.loads((target/'issuer-previous.json').read_text())
                previous[0]['lab_retire_at']=operation['switch_deadline']
                atomic_json(target/'issuer-previous.json',previous)
                validate(target)
                sequence=publisher('identity',target,directory)
                waiter('identity',lambda state:state.get('sequence',0)>=sequence and state.get('signing_kid')==active['kid'],directory)
                # Jira debe observar el plazo, no conservar la pública anterior indefinidamente.
                deadline=previous[0]['lab_retire_at']
                waiter('jira',lambda state:state.get('jwks_fresh') and state.get('key_deadlines',{}).get(previous[0]['kid'])==deadline,directory)
                phase('switched')
        elif operation['phase'] in ('prepared','announcing','switching'):
            phase('switching')
            sequence=publisher('identity',target,directory)
            waiter('identity',lambda state:state.get('sequence',0)>=sequence,directory)
            if action=='renew-certificates':
                sequence=publisher('jira',target,directory)
                waiter('jira',lambda state:state.get('sequence',0)>=sequence,directory)
            else:
                waiter('jira',lambda state:state.get('known_kids')==[active['kid']] and state.get('jwks_fresh'),directory)
            phase('switched')
        # Revalidar acknowledgements al recuperar; nunca anunciar la vieja tras switching.
        waiter('identity',lambda state:state.get('signing_kid')==active['kid'],directory)
        if action=='renew-certificates':
            from cryptography import x509
            for role in ('identity','jira'):
                serial=str(x509.load_pem_x509_certificate((target/(role+'-cert.pem')).read_bytes()).serial_number)
                waiter(role,lambda state,serial=serial:state.get('serial')==serial,directory)
        phase('delivered')
        activate(target,directory)
        if action=='rotate-signing-key':
            marker=directory/'security-last-signing-rotation.json'
            previous=json.loads(marker.read_text()) if marker.exists() else {}
            if previous.get('generation')!=target.name:
                atomic_json(marker,{'generation':target.name,'rotated_at':time.time()})
        journal.unlink()
        sync_directory(directory)
        return status(directory)
