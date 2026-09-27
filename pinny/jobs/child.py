"""Sandboxed child for one job: ``python -m pinny.jobs.child``.

The worker writes one JSON request to stdin::

    {"kind": ..., "payload": {...}, "data_dir": ..., "require_isolation": bool}

Before it reads the payload's files, the child locks itself down:

1. A new user and network namespace, so it has no network at all (only a
   loopback interface that is down). Where the kernel refuses (for
   example Ubuntu's AppArmor restriction), the worker's own sandbox
   (systemd ``PrivateNetwork=yes``) must provide it; with
   ``require_isolation`` the child checks and refuses to run otherwise.
2. ``PR_SET_PDEATHSIG``: it dies if its worker dies. Set after step 1,
   because a credential change clears it.
3. ``PR_SET_NO_NEW_PRIVS``: nothing it runs can gain privileges.
4. Resource limits: address space, CPU time, file size, open files, no
   core dumps.

It answers with one JSON line on its original stdout::

    {"ok": true, "result": ..., "isolated": true}
    {"ok": false, "code": ..., "message": ..., "status": 4xx|5xx, "isolated": ...}

Only 4xx messages (written for users) are passed on; anything else is a
generic message, with the detail on stderr, which the worker keeps as the
job's log.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import signal
import socket
import sys
import traceback

from .limits import LIMITS, MB

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000
_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38


def _libc():
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - not Linux
        return None


def _prctl(libc, option: int, arg: int) -> None:
    if libc is not None:
        libc.prctl(option, arg, 0, 0, 0)


def isolate_network(libc) -> bool:
    """Enter new user and network namespaces. True when the process has no
    network interface but loopback, whether or not unshare worked (a worker
    under systemd ``PrivateNetwork=yes`` is already isolated)."""
    try:
        if hasattr(os, "unshare"):
            os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
        elif libc is not None:
            libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNET)
    except OSError:
        pass
    return network_isolated()


def network_isolated() -> bool:
    try:
        names = {n for _, n in socket.if_nameindex()}
    except OSError:
        return False
    return names <= {"lo"}


def apply_limits(kind: str) -> None:
    lim = LIMITS[kind]
    for res, value in ((resource.RLIMIT_AS, lim.memory_mb * MB), (resource.RLIMIT_CPU, lim.cpu_s),
                       (resource.RLIMIT_FSIZE, lim.file_mb * MB), (resource.RLIMIT_NOFILE, lim.open_files),
                       (resource.RLIMIT_CORE, 0)):
        soft, hard = resource.getrlimit(res)
        if hard != resource.RLIM_INFINITY and value > hard:
            value = hard
        resource.setrlimit(res, (value, value))


def main() -> int:
    # Keep the real stdout for the answer; anything a library prints goes to stderr.
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    isolated = False

    def answer(obj) -> int:
        obj["isolated"] = isolated
        out.write(json.dumps(obj) + "\n")
        out.flush()
        return 0

    try:
        req = json.loads(sys.stdin.read())
        kind, payload, data_dir = req["kind"], req["payload"], req["data_dir"]
        if kind not in LIMITS or not isinstance(payload, dict):
            return answer({"ok": False, "code": "invalid_job", "message": "Unknown job.", "status": 400})
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return answer({"ok": False, "code": "invalid_job", "message": "Unreadable job.", "status": 400})

    libc = _libc()
    parent = req.get("parent_pid")
    isolated = isolate_network(libc)
    # After the namespace switch: a credential change clears the parent-death signal.
    _prctl(libc, _PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:  # the worker died before we could arrange to die with it (or no pid)
        return 1
    _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1)
    if req.get("require_isolation") and not isolated:
        return answer({"ok": False, "code": "sandbox_unavailable", "status": 500,
                       "message": "The scanning sandbox is not available on this server."})
    try:
        apply_limits(kind)
    except (ValueError, OSError):
        traceback.print_exc()
        return answer({"ok": False, "code": "sandbox_unavailable", "status": 500,
                       "message": "The scanning sandbox is not available on this server."})

    try:
        from . import tasks
        result = tasks.run(kind, payload, data_dir)
        return answer({"ok": True, "result": result})
    except MemoryError:
        traceback.print_exc()
        return answer({"ok": False, "code": "out_of_memory", "status": 422,
                       "message": "This page needs more memory than Pinny allows for one job."})
    except Exception as exc:  # noqa: BLE001 - reported to the worker
        traceback.print_exc()
        code = getattr(exc, "code", None)
        status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
        if not isinstance(status, int) and isinstance(exc, ValueError) and isinstance(code, str):
            status = 400  # DetectionError: a ValueError with a code, meant for users
        if isinstance(code, str) and isinstance(status, int) and 400 <= status < 500:
            return answer({"ok": False, "code": code, "message": str(exc), "status": status})
        return answer({"ok": False, "code": "job_failed", "status": 500,
                       "message": "The job failed because of an internal error."})


if __name__ == "__main__":
    sys.exit(main())
