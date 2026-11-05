FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data PORT=22362

# build-essential/zlib : compilation de libpff-python (pypff) si pas de wheel
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential zlib1g-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY app/engine/requirements.txt /app/app/engine/requirements.txt
RUN pip install -r requirements.txt -r app/engine/requirements.txt

ARG DEV=0
COPY tests/server/requirements-dev.txt /app/tests/server/requirements-dev.txt
RUN if [ "$DEV" = "1" ]; then pip install -r tests/server/requirements-dev.txt; fi

COPY . /app
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 22362

CMD ["python", "-m", "app.server.web"]
