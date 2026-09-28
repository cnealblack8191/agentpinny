"""Import the Python scripts in deploy/bin (they have no .py suffix)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "deploy" / "bin"


def load(name: str):
    modname = "pinny_deploy_" + name.replace("-", "_")
    if modname in sys.modules:
        return sys.modules[modname]
    loader = importlib.machinery.SourceFileLoader(modname, str(BIN / name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod  # dataclasses look their module up here
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in deploy/bin
    try:
        loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod
