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

    # --- eq_principle: callable as decorator factory or direct passthrough --
    def _eq_principle(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]

        def _deco(fn):
            return fn

        return _deco

    # --- the `gl` namespace -------------------------------------------------
    class _Contract:
        """Stand-in base class for gl.Contract."""

    gl = types.SimpleNamespace()
    gl.Contract = _Contract
    gl.public = _Public()
    gl.message = types.SimpleNamespace(sender_account="0xStubSenderAccount")
    gl.block = types.SimpleNamespace(timestamp=1_900_000_000)
    gl.nondet = types.SimpleNamespace(
        web=types.SimpleNamespace(
            render=lambda *a, **k: {"status": 200, "body": "", "headers": {}}
        ),
        exec_prompt=lambda *a, **k: "",
    )
    gl.eq_principle = types.SimpleNamespace(
        strict_eq=_eq_principle,
        prompt_comparative=_eq_principle,
        prompt_non_comparative=_eq_principle,
    )

    # --- storage primitives: referenced only in (stringified) annotations ---
    class _Generic:
        def __class_getitem__(cls, _item):
            return cls

    # GenLayer re-exports the stdlib dataclass plus sized-int aliases used in
    # storage structs. Map the int aliases to plain `int` and reuse stdlib
    # dataclass so @dataclass-decorated storage records construct normally.
    import dataclasses

    mod.gl = gl
    mod.Contract = _Contract
    mod.TreeMap = _Generic
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
