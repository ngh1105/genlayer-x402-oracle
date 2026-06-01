# conftest.py — test bootstrap for the x402 Paywalled-Data Oracle contract.
#
# The Intelligent Contract does `from genlayer import *` at import time, which
# is only available inside the GenVM runtime. To unit-test the *pure,
# deterministic* helper logic (host parsing, JSON coercion, 402 parsing) in a
# plain CI environment, we inject a minimal stub `genlayer` module into
# sys.modules BEFORE the contract is imported, and put `contracts/` on the path.
#
# This stub is intentionally dumb: it satisfies class creation and decorator
# evaluation so the module imports. It does NOT emulate consensus or the
# network — tests only exercise functions that are pure Python.

import os
import sys
import types


def _install_genlayer_stub() -> None:
    if "genlayer" in sys.modules:
        return

    mod = types.ModuleType("genlayer")

    # --- decorators: identity (return the function unchanged) ---------------
    class _Public:
        @staticmethod
        def write(fn):
            return fn

        @staticmethod
        def view(fn):
            return fn

    # --- eq_principle: EXECUTE the non-det closure and return its value -----
    # GenLayer runs the leader closure and reconciles validators; for unit
    # tests we faithfully CALL the closure so the contract's two-phase flow is
    # actually exercised. (The earlier passthrough stub never called it, which
    # is why resolve() was never unit-tested before.)
    def _eq_principle(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]()

        def _deco(fn):
            return fn

        return _deco

    # --- the `gl` namespace -------------------------------------------------
    # Storage-backed contract base: GenVM auto-initializes typed storage fields
    # (TreeMap/DynArray) before the contract's __init__ runs. We emulate that
    # here so unit tests can actually construct the contract and exercise its
    # state machine, not just the pure helpers.
    class _TreeMap(dict):
        """dict-backed stand-in for GenLayer's TreeMap (has .get already)."""

    class _Contract:
        """Stand-in base class for gl.Contract with auto storage init."""

        def __new__(cls, *args, **kwargs):
            self = super().__new__(cls)
            anns = {}
            for klass in reversed(cls.__mro__):
                anns.update(getattr(klass, "__annotations__", {}))
            for name, ann in anns.items():
                if ann is _TreeMapMarker:
                    setattr(self, name, _TreeMap())
            return self

    gl = types.SimpleNamespace()
    gl.Contract = _Contract
    gl.public = _Public()
    # Real SDK attribute is `sender_address` (the contract + verified studionet
    # deploy use it). Kept mutable so tests can simulate owner/relayer/other
    # senders for access-control checks.
    gl.message = types.SimpleNamespace(sender_address="0xStubSender")
    gl.block = types.SimpleNamespace(timestamp=1_900_000_000)
    gl.nondet = types.SimpleNamespace(
        web=types.SimpleNamespace(
            get=lambda *a, **k: {"status": 200, "body": "", "headers": {}},
            render=lambda *a, **k: {"status": 200, "body": "", "headers": {}},
        ),
        exec_prompt=lambda *a, **k: "",
    )
    gl.eq_principle = types.SimpleNamespace(
        strict_eq=_eq_principle,
        prompt_comparative=_eq_principle,
        prompt_non_comparative=_eq_principle,
    )

    # --- storage primitives: TreeMap is dict-backed (real, for state tests);
    #     DynArray and other generics are inert annotation placeholders. -----
    class _TreeMapMarker:
        """Sentinel left in annotations so _Contract.__new__ can init it."""

    class _TreeMapType:
        def __class_getitem__(cls, _item):
            return _TreeMapMarker

    class _Generic:
        def __class_getitem__(cls, _item):
            return cls

    # GenLayer re-exports the stdlib dataclass plus sized-int aliases used in
    # storage structs. Map the int aliases to plain `int` and reuse stdlib
    # dataclass so @dataclass-decorated storage records construct normally.
    import dataclasses

    mod.gl = gl
    mod.Contract = _Contract
    mod.TreeMap = _TreeMapType
    mod.DynArray = _Generic
    mod.Address = str
    mod.allow_storage = lambda x: x
    mod.dataclass = dataclasses.dataclass
    # Sized integer aliases (u8..u256, i8..i256) -> int for plain-Python tests.
    for _name in (
        "u8", "u16", "u32", "u64", "u128", "u256",
        "i8", "i16", "i32", "i64", "i128", "i256",
        "bigint",
    ):
        setattr(mod, _name, int)
    mod.__all__ = [
        "gl",
        "Contract",
        "TreeMap",
        "DynArray",
        "Address",
        "allow_storage",
        "dataclass",
        "u8", "u16", "u32", "u64", "u128", "u256",
        "i8", "i16", "i32", "i64", "i128", "i256",
        "bigint",
    ]

    sys.modules["genlayer"] = mod


# Make `contracts/` importable as a top-level package path.
_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTRACTS = os.path.join(_HERE, "contracts")
if _CONTRACTS not in sys.path:
    sys.path.insert(0, _CONTRACTS)

_install_genlayer_stub()
