FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY clients ./clients
# Operator scripts (create_owner_user.py --reset, digest_preview.py) must run against the volume database:
#   flyctl ssh console --app deskline-ai -C "python scripts/create_owner_user.py --reset owner@example.com"
COPY scripts ./scripts

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
