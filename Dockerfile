# syntax=docker/dockerfile:1
ARG CADDY_IMAGE=caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d
ARG OPENSEO_IMAGE=ghcr.io/every-app/open-seo:v0.1.11@sha256:0e597349873d3f94321db70348387882c5cb123873ef82a0fa827f11ac0a09fa
FROM ${CADDY_IMAGE} AS caddy
FROM ${OPENSEO_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/hosmelq/railway-openseo-template" \
      org.opencontainers.image.description="Prebuilt OpenSEO with Caddy authentication for Railway"

ENV AUTH_MODE=local_noauth \
    CLOUDFLARE_INCLUDE_PROCESS_ENV=true \
    VITE_SHOW_DEVTOOLS=false

WORKDIR /app
# Build the official application as supplied, without patches or migrations.
RUN CI=true OPENSEO_TELEMETRY_DISABLED=1 pnpm run build \
    && node -e "const fs=require('node:fs');fs.mkdirSync('/data',{recursive:true});fs.rmSync('.wrangler/state',{recursive:true,force:true});fs.symlinkSync('/data/state','.wrangler/state')"

# Vite snapshots build-time bindings into .dev.vars. Preview must instead
# consume the deployer's runtime environment through CLOUDFLARE_INCLUDE_PROCESS_ENV.
RUN node -e "const fs=require('node:fs'),path=require('node:path');function clean(dir){for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,entry.name);if(entry.isDirectory())clean(file);else if(entry.isFile()&&entry.name.startsWith('.dev.vars'))fs.unlinkSync(file)}}clean('dist')"

ENV ACCESS_USERNAME=admin
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY Caddyfile build-fingerprint.mjs railway-entrypoint.mjs /opt/railway/
RUN node /opt/railway/build-fingerprint.mjs write

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "/opt/railway/railway-entrypoint.mjs"]
