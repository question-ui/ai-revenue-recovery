.PHONY: install run dev test docker

install:
	pip install -r requirements.txt

run:
	python run.py

dev:
	RELOAD=1 python run.py

test:
	python -m pytest -q

docker:
	docker compose up --build
