FROM python:3.12-slim

WORKDIR /app

# curl voor de health-check van Coolify: die draait curl of wget in de container,
# en python:3.12-slim heeft geen van beide.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
