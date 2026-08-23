FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# API seule (le crawler nocturne tourne via GitHub Actions ou cron local :
#   docker run --rm -e DATABASE_URL=... prixtn python crawler.py )
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
