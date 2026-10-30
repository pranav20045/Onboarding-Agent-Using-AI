# CodeAtlas web UI (Next.js standalone).  Build from the repo root:
#   docker build -f frontend.Dockerfile --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 -t codeatlas-frontend .
FROM node:24.11.0-bookworm-slim AS build
COPY docker/certs/ /usr/local/share/ca-certificates/extra/
RUN cat /usr/local/share/ca-certificates/extra/*.crt > /tmp/extra-ca.pem 2>/dev/null || true
ENV NODE_EXTRA_CA_CERTS=/tmp/extra-ca.pem NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# NEXT_PUBLIC_* values are inlined into the browser bundle at build time
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npx next build

FROM node:24.11.0-bookworm-slim
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
