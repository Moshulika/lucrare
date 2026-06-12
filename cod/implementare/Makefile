PYTHON := uv run python
UV := uv
VENV := .venv

.PHONY: setup dev-setup start test ui old_ui format help web eval install

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install locally
	sh scripts/install.sh local

setup: ## Install project dependencies
	@[ -d $(VENV) ] || $(UV) venv $(VENV)
	$(UV) sync
	@echo ""
	@echo "----------------------------------------------"
	@echo "  Setup complete!"
	@echo ""
	@echo "  Activate the venv:  source $(VENV)/bin/activate"
	@echo "  Then run:           make start"
	@echo "----------------------------------------------"

dev-setup: ## Install project in dev mode with all tooling
	@[ -d $(VENV) ] || $(UV) venv $(VENV)
	$(UV) sync --all-groups
	$(UV) run pre-commit install
	@echo ""
	@echo "----------------------------------------------"
	@echo "  Dev setup complete!"
	@echo ""
	@echo "  Activate the venv:  source $(VENV)/bin/activate"
	@echo "  Run the agent:      make start"
	@echo "  Run the TUI:        make ui"
	@echo "  Run tests:          make test"
	@echo "  Format code:        make format"
	@echo "----------------------------------------------"

start: ## Run the agent in the terminal
	$(PYTHON) -m src.main

test: ## Run the test suite with pytest
	$(UV) run pytest

old_ui: ## Launch the Textual TUI
	$(PYTHON) -m ui.app_old

ui: ## Launch the prompt_toolkit + rich chat UI
	$(PYTHON) -m ui.app

web: ## Launch the analytics web panel (http://127.0.0.1:5050)
	$(UV) run --with flask --with psutil python -m web.app

eval: ## Run the evaluation suite (config/eval.yml)
	$(UV) run --extra eval python -m eval

format: ## Check and format code with ruff
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
