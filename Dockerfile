# UI build: the result goes to app/web in the final image. Node is not in the final image.
FROM node:26-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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

# Only the application: the UI sources stay out of the image, their build comes from the stage above.
COPY app /app/app
COPY --from=web /build/app/web /app/app/web
# Command line (standard library only): `docker compose exec web mccb status`, or copy it to a NAS or server
RUN chmod 755 /app/app/cli/mccb.py && ln -s /app/app/cli/mccb.py /usr/local/bin/mccb
# Version: APP_VERSION = VERSION if given; otherwise app/VERSION (+GIT_SHA if given), resolved by
# app/server/version.py from the (possibly empty) variables below.
ARG VERSION=
ARG GIT_SHA=
ENV APP_VERSION=${VERSION} GIT_SHA=${GIT_SHA}
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 22362

CMD ["python", "-m", "app.server.web"]
