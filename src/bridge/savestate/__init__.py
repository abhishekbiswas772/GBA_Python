"""Save states for every core: snapshots in memory (rewind, this session's slots) and on disk.

A snapshot is the core's whole object graph pickled, minus:

* the big read-only buffers (ROM, BIOS), passed by reference through
  ``Machine.state_blobs()`` so a state stays small;
* the transient helpers each core lists in ``_NOT_STATE`` (generated-code
  closures, memoryviews, caches); see :mod:`src.core.adaptors.snapshot`.

Two ways back:

* **in place** (:func:`restore`, used by rewind and by slots saved in this
  session).  Each object recorded while pickling is paired with its live
  counterpart by pickle's memo order, and its state is copied back into that
  same object.  Generated code, caches and memoryviews stay valid, so there
  is no stall.
* **adopt** (:func:`load_file`, a state from disk).  The unpickled graph
  becomes the machine's core, and each core rebuilds its helpers once.

Disk states are loaded with a restricted unpickler: only classes from
``src.core`` and a few safe builtins are allowed, so a shared ``.state``
file can't run code.
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import pickle
import time
import types
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import src.core.adaptors.snapshot as hooks

MAGIC = b"GBPYSTATE\x01"
VERSION = 1
_MUTABLE = (list, dict, bytearray, array.array, set)


class _Keep:
    """Placeholder for a value that can't be pickled (a local closure): the live one is kept."""

    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()


# ------------------------------------------------------------------ pickling
class _Pickler(pickle.Pickler):
    def __init__(self, f, blobs):
        super().__init__(f, protocol=5)
        self._blobs = {id(b): i for i, b in enumerate(blobs)}

    def persistent_id(self, obj):
        i = self._blobs.get(id(obj))
        if i is not None:
            return ("blob", i)
        if isinstance(obj, types.FunctionType) and "<" in obj.__qualname__:
            return ("keep",)                      # lambda / closure: the live core keeps its own
        return None


class _Unpickler(pickle.Unpickler):
    def __init__(self, f, blobs, restricted: bool):
        super().__init__(f)
        self._blobs = blobs
        self._restricted = restricted

    def persistent_load(self, pid):
        if pid[0] == "blob":
            return self._blobs[pid[1]]
        return KEEP

    _SAFE = {
        "builtins": {"bytearray", "set", "frozenset", "complex", "range", "slice", "bytes",
                     "list", "dict", "tuple", "object", "getattr", "int", "float", "bool", "str"},
        "array": {"array", "_array_reconstructor"},
        "copyreg": {"_reconstructor"},
        "collections": {"deque", "OrderedDict"},
        "pathlib": {"Path", "PurePath", "WindowsPath", "PosixPath", "PureWindowsPath", "PurePosixPath"},
        "pathlib._local": {"Path", "PurePath", "WindowsPath", "PosixPath", "PureWindowsPath", "PurePosixPath"},
    }

    def find_class(self, module, name):
        if self._restricted and not (module.startswith("src.core.") or name in self._SAFE.get(module, ())):
            raise pickle.UnpicklingError(f"save state refers to {module}.{name}, which is not allowed")
        return super().find_class(module, name)


_NOT_INSTANCES = (type, types.FunctionType, types.BuiltinFunctionType, types.MethodType, types.ModuleType)


def _stateful(obj) -> bool:
    """Objects whose contents a restore copies back in place."""
    if isinstance(obj, _MUTABLE):
        return True
    return hasattr(obj, "__dict__") and not isinstance(obj, _NOT_INSTANCES)


def _dump(core, blobs) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    p = _Pickler(buf, blobs)
    p.dump(core)
    live = {i: obj for i, obj in p.memo.copy().values() if _stateful(obj)}
    return buf.getvalue(), live


# ------------------------------------------------------------------ in-memory snapshots
@dataclass
class Snapshot:
    data: bytes                       # zlib-compressed pickle
    live: dict                        # memo index -> the live object it was taken from
    system: str
    frame: int = 0
    taken: float = field(default_factory=time.time)
    root: object = None               # the core object it was taken from

    @property
    def size(self) -> int:
        return len(self.data)


def capture(machine, frame: int = 0, level: int = 1) -> Snapshot:
    """Snapshot the running machine (call between frames, on the emulator thread)."""
    raw, live = _dump(machine.core, machine.state_blobs())
    return Snapshot(zlib.compress(raw, level), live, machine.system, frame, root=machine.core)


def restore(machine, snap: Snapshot) -> None:
    """Put the machine back to `snap`, in place (same objects, same compiled code)."""
    if snap.system != machine.system:
        raise ValueError(f"state is for {snap.system}, not {machine.system}")
    sampling = machine.apu.sampling
    raw = zlib.decompress(snap.data)
    if snap.root is not machine.core:            # core was replaced since (reset / disk load)
        _adopt(machine, raw, restricted=False)
    else:
        hooks.REBUILD = False
        try:
            u = _Unpickler(io.BytesIO(raw), machine.state_blobs(), False)
            u.load()
            memo = u.memo.copy()
        finally:
            hooks.REBUILD = True
        _merge(memo, snap.live)
    _resume_audio(machine, sampling)


def _resume_audio(machine, sampling: bool) -> None:
    """Keep the live sampling setting (setting it resets the sample clock, so only
    when it changed) and drop audio buffered at the snapshot moment."""
    apu = machine.apu
    if bool(apu.sampling) != bool(sampling):
        apu.sampling = sampling
    apu.take_samples()


def _merge(new_memo: dict, live: dict) -> None:
    pair: dict[int, object] = {}
    for i, live_obj in live.items():
        new_obj = new_memo.get(i)
        if new_obj is not None and type(new_obj) is type(live_obj):
            pair[id(new_obj)] = live_obj
    instance_dicts = {id(o.__dict__) for o in live.values() if hasattr(o, "__dict__")}

    def tr(v):
        o = pair.get(id(v))
        if o is not None:
            return o
        t = type(v)
        if t is tuple:
            return tuple(tr(x) for x in v)
        if t.__name__ == "method":                # bound method of a restored object
            owner = pair.get(id(v.__self__))
            if owner is not None:
                return getattr(owner, v.__func__.__name__)
        return v

    for i, live_obj in live.items():
        new_obj = new_memo.get(i)
        if new_obj is None or type(new_obj) is not type(live_obj):
            continue
        if isinstance(live_obj, (bytearray, array.array)):
            live_obj[:] = new_obj
        elif isinstance(live_obj, list):
            old = live_obj[:]
            live_obj[:] = [(old[j] if j < len(old) else None) if x is KEEP else tr(x)
                           for j, x in enumerate(new_obj)]
        elif isinstance(live_obj, dict):
            if id(live_obj) in instance_dicts:
                continue                          # handled through its object below
            vals = {k: live_obj.get(k) if x is KEEP else tr(x) for k, x in new_obj.items()}
            live_obj.clear()
            live_obj.update(vals)
        elif isinstance(live_obj, set):
            live_obj.clear()
            live_obj.update(new_obj)
        elif hasattr(live_obj, "__dict__"):
            skip = getattr(type(live_obj), "_NOT_STATE", ())
            d = live_obj.__dict__
            for k, x in new_obj.__dict__.items():
                if k in skip or x is KEEP:
                    continue
                d[k] = tr(x)


# ------------------------------------------------------------------ files
def _rom_id(machine) -> str:
    blobs = machine.state_blobs()
    return hashlib.sha1(blobs[0]).hexdigest() if blobs else ""


def save_file(machine, path, *, thumbnail: bytes | None = None, title: str = "") -> dict:
    """Write a state file (atomic). Returns its metadata."""
    raw, _ = _dump(machine.core, machine.state_blobs())
    meta = {"version": VERSION, "system": machine.system, "rom": _rom_id(machine),
            "title": title, "time": time.time()}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    head = json.dumps(meta).encode()
    tmp.write_bytes(MAGIC + len(head).to_bytes(4, "little") + head + zlib.compress(raw, 6))
    tmp.replace(path)
    if thumbnail:
        path.with_suffix(".png").write_bytes(thumbnail)
    return meta


def read_meta(path) -> dict | None:
    try:
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                return None
            n = int.from_bytes(f.read(4), "little")
            return json.loads(f.read(n))
    except (OSError, ValueError):
        return None


def load_file(machine, path) -> None:
    """Load a state file into `machine` (it must be the same game)."""
    blob = Path(path).read_bytes()
    if not blob.startswith(MAGIC):
        raise ValueError("not a save state file")
    n = int.from_bytes(blob[len(MAGIC):len(MAGIC) + 4], "little")
    meta = json.loads(blob[len(MAGIC) + 4:len(MAGIC) + 4 + n])
    if meta.get("system") != machine.system:
        raise ValueError(f"this state is for {meta.get('system')}, not {machine.system}")
    if meta.get("rom") and meta["rom"] != _rom_id(machine):
        raise ValueError("this state was saved from a different game")
    sampling = machine.apu.sampling
    _adopt(machine, zlib.decompress(blob[len(MAGIC) + 4 + n:]), restricted=True)
    _resume_audio(machine, sampling)


def _adopt(machine, raw: bytes, restricted: bool) -> None:
    """Unpickle a full core (helpers rebuilt) and make it the machine's core."""
    hooks._PENDING.clear()
    core = _Unpickler(io.BytesIO(raw), machine.state_blobs(), restricted).load()
    hooks.finish_rebuild()
    old, new = getattr(machine.core, "cart", None), getattr(core, "cart", None)
    if old is not None and new is not None:      # keep this session's ROM / .sav locations
        for k in ("path", "sav_path", "save_ram"):
            if k in old.__dict__:
                new.__dict__[k] = old.__dict__[k]
    machine.adopt_core(core)


# ------------------------------------------------------------------ slots
SLOTS = 10


class SlotStore:
    """Numbered save-state slots for one game, kept in the game's save folder:
    ``saves/<core>/<game>/states/slot<N>.state`` (+ ``.png`` thumbnail)."""

    def __init__(self, rom_path, core: str):
        from src.core.adaptors.saves import save_dir
        rom = Path(rom_path)
        root = save_dir()
        self.folder = (root / core / rom.stem / "states") if root else rom.with_name(rom.stem + ".states")
        self.title = rom.stem

    def path(self, slot: int) -> Path:
        return self.folder / f"slot{slot}.state"

    def save(self, machine, slot: int, thumbnail: bytes | None = None) -> dict:
        return save_file(machine, self.path(slot), thumbnail=thumbnail, title=self.title)

    def load(self, machine, slot: int) -> None:
        p = self.path(slot)
        if not p.is_file():
            raise FileNotFoundError(f"slot {slot} is empty")
        load_file(machine, p)

    def delete(self, slot: int) -> None:
        for p in (self.path(slot), self.path(slot).with_suffix(".png")):
            p.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        """One entry per slot: {"slot", "path", "time", "thumb"} (time None = empty)."""
        out = []
        for s in range(SLOTS):
            p = self.path(s)
            meta = read_meta(p) if p.is_file() else None
            thumb = p.with_suffix(".png")
            out.append({"slot": s, "path": p, "time": meta and meta.get("time"),
                        "thumb": thumb if thumb.is_file() else None})
        return out

    def latest(self) -> dict | None:
        used = [e for e in self.list() if e["time"]]
        return max(used, key=lambda e: e["time"]) if used else None
