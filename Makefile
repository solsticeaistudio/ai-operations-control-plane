run:
	uvicorn app.main:app --reload --port 8080

test:
	pytest -q

seed:
	curl -X POST 'http://localhost:8080/v1/demo/seed?clear=true'
