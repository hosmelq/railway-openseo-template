ARG CADDY_IMAGE=caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
ARG OPENSEO_IMAGE=ghcr.io/every-app/open-seo-standalone:v0.1.30@sha256:2b2dd143b86157e700938ddfcfefda2a8286fa1d75e2d9052b930ea4b694f323
FROM ${CADDY_IMAGE} AS caddy
FROM ${OPENSEO_IMAGE}

LABEL org.opencontainers.image.description="Prebuilt OpenSEO for Railway, backed by a Railway Bucket" \
      org.opencontainers.image.source="https://github.com/hosmelq/railway-openseo-template"

USER root
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY Caddyfile railway-entrypoint.mjs /opt/railway/
RUN rm -rf /config && ln -s /data/config /config

ENV CELLD_WATCH=/data/celld \
    CELLD_MAX_RSS_MB=1536 \
    CELLD_MAX_RESIDENT_CELLS=64 \
    CELLD_V8_HEAP_LIMIT_MB=256 \
    CELLD_MAX_REQUEST_BODY_BYTES=33554432 \
    RUST_LOG=warn,cell_console=info

EXPOSE 3000
CMD ["node", "/opt/railway/railway-entrypoint.mjs"]
