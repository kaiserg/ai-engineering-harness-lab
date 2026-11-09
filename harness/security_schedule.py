"""Planificador del laboratorio: reloj persistido, exclusión mutua y reanudación."""
from datetime import datetime, timezone
import json
import time
from .postgres_config import RUNTIME
from .security_lifecycle import atomic_json, status, operation_lock
from .live_security import perform_live


def configure(directory=RUNTIME):
    path=directory/'security-schedule.json'
    if not path.exists():
        atomic_json(path,{'enabled':True,'interval_seconds':60,'renew_before_days':7,
            'rotate_every_days':30,'last_rotation':time.time(),'next_check':0,
            'last_action':None,'last_error':None,'failures':0})
    return json.loads(path.read_text())


def tick(directory=RUNTIME, *, now=None, executor=perform_live):
    now=time.time() if now is None else now
    # Un lock separado serializa ticks; el ejecutor usa el lock común de infraestructura.
    import fcntl, os
    fd=os.open(directory/'security-scheduler-tick.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=configure(directory)
        if not state['enabled'] or state['next_check']>now:
            return state
        action=None
        try:
            current=status(directory)
            recovered_action=None
            if (directory/'security-live-operation.json').exists():
                recovered_action=json.loads((directory/'security-live-operation.json').read_text()).get('action')
                action='recover'
            elif (directory/'security-operation.json').exists():
                raise ValueError('Coordinated maintenance is pending')
            elif any(datetime.fromisoformat(cert['expires_at']).timestamp()-now <= state['renew_before_days']*86400
                    for cert in current['certificates'].values()):
                action='renew-certificates'
            elif current['previous'] and all(item['retire_at']<=now for item in current['previous']):
                action='retire-signing-key'
            elif not current['previous'] and now-max(state['last_rotation'],
                    json.loads((directory/'security-last-signing-rotation.json').read_text())['rotated_at']
                    if (directory/'security-last-signing-rotation.json').exists() else state['last_rotation'])>=state['rotate_every_days']*86400:
                action='rotate-signing-key'
            if action:
                executor(action,directory)
                if action=='rotate-signing-key' or recovered_action=='rotate-signing-key':
                    state['last_rotation']=now
            state.update(last_action=action,last_error=None,failures=0,next_check=now+state['interval_seconds'])
        except Exception:
            failures=state['failures']+1
            state.update(last_action=action,last_error='SECURITY_SCHEDULE_FAILED',failures=failures,
                next_check=now+min(900,60*2**min(failures-1,4)))
        state['last_check']=now
        atomic_json(directory/'security-schedule.json',state)
        return state
    finally:
        os.close(fd)
