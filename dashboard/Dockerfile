# ─── Build Stage ─────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app

# Accept gateway URLs as build args so Next.js bakes the correct destinations
# into the standalone bundle (defaults: Docker Compose service names).
# GATEWAY_INTERNAL_URL  — used by Next.js rewrites (server-side API proxy).
# NEXT_PUBLIC_GATEWAY_URL — used by the browser for SSE (bypasses Next.js proxy).
ARG GATEWAY_INTERNAL_URL=http://gateway:8000
ARG NEXT_PUBLIC_GATEWAY_URL=http://localhost:8000
ENV GATEWAY_INTERNAL_URL=$GATEWAY_INTERNAL_URL
ENV NEXT_PUBLIC_GATEWAY_URL=$NEXT_PUBLIC_GATEWAY_URL

COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

# ─── Production Stage ────────────────────────────────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]
