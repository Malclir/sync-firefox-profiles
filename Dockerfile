ARG FFSCLIENT_VERSION=v1.9.0

FROM golang:1.24-alpine AS ffsclient-builder

ARG FFSCLIENT_VERSION

RUN apk add --no-cache git
RUN git clone --depth 1 --branch "${FFSCLIENT_VERSION}" https://github.com/Mikescher/firefox-sync-client.git /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /ffsclient ./cmd/ffsclient

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffsclient-builder /ffsclient /usr/local/bin/ffsclient
RUN /usr/local/bin/ffsclient --version

WORKDIR /app
COPY bridge.py /app/bridge.py
COPY bootstrap.py /app/bootstrap.py
COPY config.example.json /app/config.example.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV BRIDGE_CONFIG=/config/config.json
VOLUME ["/config"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["loop"]
