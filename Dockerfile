FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Single worker, generous timeout. A 268 MB score has to be pulled from Drive,
# hashed, rendered and pushed to GCS inside one request, and the whole design
# assumes that is allowed to take a while rather than be raced.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 900 app.main:app
