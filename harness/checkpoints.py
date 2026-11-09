"""Pausas didácticas tras commits, compartidas por la ejecución local y el worker."""
class RunInterrupted(BaseException):
    pass


def pause_hook(pause_at):
    def checkpoint(point, run):
        count = {'jira': 1, 'github': 2, 'jenkins': 3}.get(pause_at)
        if ((point == 'after-result' and len(run.results) == count)
                or (point == 'after-tool-failure' and pause_at == 'failure')):
            raise RunInterrupted()
    return checkpoint
