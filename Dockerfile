FROM python:3.12-slim

# fonts for headline graphics
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
ENV NEWSROOM_DATA=/data PYTHONUNBUFFERED=1 NEWSROOM_HTTPS=1
RUN useradd -m -u 1000 newsroom && mkdir -p /data && chown newsroom /data
USER newsroom

EXPOSE 8000
CMD ["gunicorn", "--workers", "3", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8000", "app.wsgi:app"]
