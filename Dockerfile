FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data PORT=22362

# build-essential/zlib: to compile libpff-python (pypff) when no wheel is available
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
# Version: APP_VERSION = VERSION if given; otherwise app/VERSION (+GIT_SHA if given), resolved by
# app/server/version.py from the (possibly empty) variables below.
ARG VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${VERSION} GIT_SHA=${GIT_SHA}
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 22362

CMD ["python", "-m", "app.server.web"]
