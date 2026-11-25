# Programm-Abbild für den Server-Betrieb (siehe BETRIEB.md und docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=server \
    DATA_DIR=/daten

# Texterkennung (OCR) mit deutschen Sprachdaten
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Nicht als root laufen
RUN useradd --create-home --uid 1000 buchhaltung \
    && mkdir -p /daten && chown buchhaltung:buchhaltung /daten
USER buchhaltung

EXPOSE 8000
# Datenbank migrieren, noch offene Belege verschlüsseln (app/services/tresor.py), dann starten. Der Port 8000 ist nur im internen Docker-Netz erreichbar (vor Caddy),
# deshalb dürfen die Weiterleitungs-Header (echte Besucher-IP) von dort übernommen werden.
CMD ["sh", "-c", "python -m alembic upgrade head && python -m app.services.tresor && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
