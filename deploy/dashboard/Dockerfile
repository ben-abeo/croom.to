# syntax=docker/dockerfile:1
# Crystal Meet dashboard image: the web app and the backend, built from the repo root.

FROM node:20-bookworm-slim AS frontend-build
WORKDIR /src/frontend
COPY src/croom-dashboard/frontend/package.json src/croom-dashboard/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/croom-dashboard/frontend/ ./
RUN npx vite build

FROM node:20-bookworm-slim AS backend-build
# bcrypt ships prebuilt binaries for arm64 and x86_64; the toolchain is the fallback.
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /src/backend
COPY src/croom-dashboard/backend/package.json src/croom-dashboard/backend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/croom-dashboard/backend/ ./
RUN npm run build && npm prune --omit=dev

FROM node:20-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
COPY --from=backend-build /src/backend/dist ./dist
COPY --from=backend-build /src/backend/node_modules ./node_modules
COPY --from=backend-build /src/backend/package.json ./package.json
COPY --from=frontend-build /src/frontend/dist ./public
USER node
EXPOSE 3001
CMD ["node", "dist/index.js"]
