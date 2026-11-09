"""Esperar el reloj autoritativo de PostgreSQL, no asumir sincronía con el host/VM."""
import time


def wait_for_expired_lease(runs,identifier,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        with runs._database() as db:
            row=db.execute('SELECT lease_until<=clock_timestamp() AS expired FROM operations WHERE operation_id=%s',
                (identifier,)).fetchone()
        if row and row['expired']:return
        time.sleep(.1)
    raise AssertionError('PostgreSQL no confirmó el vencimiento del lease dentro del plazo de prueba.')
