"""Un dueño local por base: CLI y API no recuperan el mismo run simultáneamente."""
from contextlib import contextmanager
import fcntl
from pathlib import Path

from .contracts import ContractError


@contextmanager
def database_owner(path):
    lock = Path(str(Path(path).resolve()) + '.owner.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError('DATABASE_IN_USE') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
