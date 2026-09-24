# Pinny

Local prototype for detecting receptacle pins on electrical drawing PDFs.

* Contracts: `docs/contracts.md`
* Viewer: `docs/viewer.md`
* Phase 2 (learning models, branch `phase-2`): `docs/phase2-contracts.md`, `docs/phase2-ownership.md`
* Ownership: `docs/agent-ownership.md`
* Render service (foundation): `docs/render-service.md`

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m pinny.viewer      # then open http://127.0.0.1:8765/
```
