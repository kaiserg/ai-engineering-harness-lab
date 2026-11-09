"""Renovación y rotación en vivo, con planificador local explícito."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.postgres_config import RUNTIME
from harness.security_lifecycle import ACTIONS, operation_lock, status, atomic_json
from harness.live_security import perform_live
from harness.security_schedule import configure, tick


def daemon():
    import fcntl
    fd=os.open(RUNTIME/'security-scheduler-daemon.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        atomic_json(RUNTIME/'security-scheduler.pid.json',{'pid':os.getpid(),'protocol':2})
        stopping=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stopping.set())
        signal.signal(signal.SIGINT,lambda *_:stopping.set())
        while not stopping.is_set() and not (RUNTIME/'security-scheduler.stop').exists():
            try:
                tick()
            except BlockingIOError:
                pass
            stopping.wait(1)
    finally:
        os.close(fd)


def schedule_start():
    state=configure()
    if scheduler_running():
        metadata=json.loads((RUNTIME/'security-scheduler.pid.json').read_text())
        if metadata.get('protocol')!=2:
            raise ValueError('The legacy test scheduler must be stopped in your terminal')
    state['enabled']=True
    atomic_json(RUNTIME/'security-schedule.json',state)
    (RUNTIME/'security-scheduler.stop').unlink(missing_ok=True)
    # El lock de daemon impide duplicados aunque dos lanzadores compitan.
    if scheduler_running():
        return
    with (RUNTIME/'security-scheduler.log').open('ab') as output:
        subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--daemon'],
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    for _ in range(50):
        if scheduler_running():
            return
        time.sleep(.1)
    raise ValueError('Scheduler did not start')


def scheduler_running():
    import fcntl
    fd=os.open(RUNTIME/'security-scheduler-daemon.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def schedule_stop():
    state=configure()
    state['enabled']=False
    atomic_json(RUNTIME/'security-schedule.json',state)
    atomic_json(RUNTIME/'security-scheduler.stop',{'stop':True})
    until=time.monotonic()+45
    while scheduler_running() and time.monotonic()<until:
        time.sleep(.1)
    if scheduler_running():
        raise ValueError('Scheduler is still completing an operation')
    state=configure()
    state['enabled']=False
    atomic_json(RUNTIME/'security-schedule.json',state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=['status',*ACTIONS,'recover','schedule-start','schedule-stop','schedule-once'])
    parser.add_argument('--daemon',action='store_true')
    args=parser.parse_args()
    if args.daemon:
        return daemon()
    if args.action=='schedule-start':
        schedule_start()
    elif args.action=='schedule-stop':
        schedule_stop()
    elif args.action=='schedule-once':
        print(json.dumps(tick(),indent=2))
    elif args.action=='status':
        result=status()
        result['live_operation']=json.loads((RUNTIME/'security-live-operation.json').read_text()) if (RUNTIME/'security-live-operation.json').exists() else None
        result['schedule']=configure()
        result['scheduler_running']=scheduler_running()
        print(json.dumps(result,indent=2))
    elif args.action:
        print(json.dumps(perform_live(args.action),indent=2))
    else:
        parser.error('An action is required')


if __name__=='__main__':
    try:
        main()
    except (ValueError,OSError,subprocess.CalledProcessError):
        print('LIVE_SECURITY_FAILED: inspect status and recover the pending live operation.',file=sys.stderr)
        raise SystemExit(1)
