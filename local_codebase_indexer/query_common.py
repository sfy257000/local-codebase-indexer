"""Consistent query snapshots shared by the facade and navigation tools."""
from functools import wraps

from . import db as dbmod


def _consistent_read(*, refresh: bool = False):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            if refresh:
                self._ensure()
            with dbmod.read_snapshot(self.conn, self.indexer.db_path):
                return method(self, *args, **kwargs)
        return wrapped
    return decorate

