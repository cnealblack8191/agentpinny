# Pinny

Local prototype for detecting receptacle pins on electrical drawing PDFs.

* Contracts: `docs/contracts.md` (coordinator branch)
* Ownership: `docs/agent-ownership.md`
* Render service (foundation): `docs/render-service.md`

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m pytest
```
