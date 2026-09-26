PY ?= backend/.venv/Scripts/python
CW ?= backend/.venv/Scripts/callwiz
.PHONY: install test dev seed migrate loadtest call rag docker
install: ; python -m venv backend/.venv && $(PY) -m pip install -e "backend[dev,docs,mcp,embeddings]"
test: ; cd backend && .venv/Scripts/python -m pytest -q
migrate: ; cd backend && ../$(CW) db migrate
seed: ; cd backend && ../$(CW) db seed
dev: ; cd backend && ../$(CW) dev
call: ; cd backend && ../$(CW) call test --agent inbound
rag: ; cd backend && ../$(CW) rag query "Quelle est votre politique de retour ?"
loadtest: ; cd backend && ../$(CW) loadtest --calls 100
docker: ; docker compose up --build
