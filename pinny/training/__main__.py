"""``python -m pinny.training <command> [args]`` (docs/phase2-ownership.md).

Each command maps to ``("module:function", help)``; the function takes
``argv`` and returns an exit code. Modules are imported only when their command runs, so
torch-based trainers never load for dataset commands. To add a command,
add one line to ``COMMANDS``.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import import_module
from pathlib import Path

from pinny.errors import PinnyError

# command -> ("module:function", one-line help). The function takes argv and returns an exit code.
COMMANDS: dict[str, tuple[str, str]] = {
    "build-dataset": ("pinny.training.__main__:build_dataset_main", "build a dataset from the learning store"),
    "synthesize": ("pinny.training.__main__:synthesize_main", "generate a synthetic labelled dataset"),
    "dataset-info": ("pinny.training.__main__:dataset_info_main", "summarise and verify a dataset"),
}


def _data_dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=None, help="default: $PINNY_DATA_DIR or ~/.local/share/pinny")


def _datasets_root(a: argparse.Namespace) -> Path:
    from .dataset import default_datasets_root

    return Path(a.out_dir) if getattr(a, "out_dir", None) else default_datasets_root(a.data_dir)


def _print_result(res) -> None:
    from .dataset import summarize

    info = summarize(res.manifest)
    info["path"] = str(res.path)
    info["reused"] = res.reused
    print(json.dumps(info, indent=2, sort_keys=True))


def build_dataset_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pinny.training build-dataset", description=COMMANDS["build-dataset"][1])
    _data_dir_arg(p)
    p.add_argument("--export", help="use this export JSON instead of exporting the store now")
    p.add_argument("--document-version", help="only this document version (sha256:...)")
    p.add_argument("--out-dir", help="datasets root (default: <data-dir>/datasets)")
    p.add_argument("--created-at", help="override created_at (default: newest source timestamp)")
    a = p.parse_args(argv)

    from pinny.learning import LearningStore, default_data_dir
    from pinny.render import RenderService

    from .dataset import build_dataset

    data_dir = Path(a.data_dir) if a.data_dir else default_data_dir()
    if a.export:
        export = json.loads(Path(a.export).read_text(encoding="utf-8"))
    else:
        with LearningStore(data_dir) as store:
            export = store.export(document_version=a.document_version)
    res = build_dataset(export, RenderService(data_dir), _datasets_root(a), created_at=a.created_at)
    _print_result(res)
    return 0


def synthesize_main(argv: list[str]) -> int:
    from .synthetic import SynthParams, synthesize_dataset

    d = SynthParams()
    p = argparse.ArgumentParser(prog="pinny.training synthesize", description=COMMANDS["synthesize"][1])
    _data_dir_arg(p)
    p.add_argument("--out-dir", help="datasets root (default: <data-dir>/datasets)")
    p.add_argument("--documents", type=int, default=6)
    p.add_argument("--pages-per-document", type=int, default=2)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--page-size", default=f"{d.page_width_pt:g}x{d.page_height_pt:g}",
                   help="page size in PDF points, WxH (default: letter)")
    p.add_argument("--noise-sigma", type=float, default=d.noise_sigma)
    p.add_argument("--created-at", help="override created_at (default: $SOURCE_DATE_EPOCH or the epoch)")
    a = p.parse_args(argv)
    try:
        w, h = (float(v) for v in a.page_size.lower().split("x"))
    except ValueError:
        p.error("--page-size must look like 612x792")
    params = SynthParams(page_width_pt=w, page_height_pt=h, noise_sigma=a.noise_sigma)
    res = synthesize_dataset(_datasets_root(a), documents=a.documents, pages_per_document=a.pages_per_document,
                             seed=a.seed, params=params, created_at=a.created_at)
    _print_result(res)
    return 0


def dataset_info_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pinny.training dataset-info", description=COMMANDS["dataset-info"][1])
    _data_dir_arg(p)
    p.add_argument("dataset", help="dataset directory, manifest.json, or dataset id")
    p.add_argument("--no-verify", action="store_true", help="skip checking files and hashes")
    a = p.parse_args(argv)

    from .dataset import default_datasets_root, load_manifest, resolve_dataset, summarize, verify_dataset

    path = resolve_dataset(a.dataset, default_datasets_root(a.data_dir))
    info = summarize(load_manifest(path))
    info["path"] = str(path)
    problems = [] if a.no_verify else verify_dataset(path)
    if not a.no_verify:
        info["problems"] = problems
    print(json.dumps(info, indent=2, sort_keys=True))
    return 1 if problems else 0


def _usage() -> str:
    width = max(map(len, COMMANDS))
    lines = ["usage: python -m pinny.training <command> [args]", "", "commands:"]
    lines += [f"  {name.ljust(width)}  {help_}" for name, (_, help_) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"unknown command {name!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    module, func = COMMANDS[name][0].split(":")
    # Under `python -m` this file is `__main__`; don't import it a second time.
    this = __spec__.name if __spec__ is not None else __name__
    mod = sys.modules[__name__] if module in (this, __name__) else import_module(module)
    try:
        return int(getattr(mod, func)(rest) or 0)
    except PinnyError as e:
        print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
