"""Save-state support shared by the cores (used by src/bridge/savestate).

A core is saved by pickling its object graph.  Most objects pickle as they
are; a few hold *transient* helpers that can't or shouldn't be pickled,
such as generated opcode closures, memoryviews or code caches.  Those
classes inherit :class:`Snapshottable` and list the attribute names in
``_NOT_STATE``:

* saving drops those attributes;
* restoring into a fresh object (a state loaded from disk) calls
  ``_rebuild_transient()`` to recreate them;
* restoring in place into the running core (rewind, slots of this session)
  sets :data:`REBUILD` to False.  The live object then keeps its own helpers,
  and only the real state is copied in.
"""
from __future__ import annotations

REBUILD = True          # flipped by the bridge while unpickling for an in-place restore
_PENDING: list = []     # restored objects waiting for _rebuild_transient()


def finish_rebuild() -> None:
    """Rebuild transient helpers once the whole graph is loaded (an object's
    helpers may need other objects that pickle restores after it)."""
    while _PENDING:
        _PENDING.pop(0)._rebuild_transient()


class Snapshottable:
    _NOT_STATE: tuple[str, ...] = ()

    def __getstate__(self):
        state = self.__dict__.copy()
        for k in self._NOT_STATE:
            state.pop(k, None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if REBUILD:
            _PENDING.append(self)

    def _rebuild_transient(self) -> None:
        """Recreate the ``_NOT_STATE`` attributes from the restored state."""
