FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
ARG ALITA_API_URL=http://alita-backend:8080
ENV ALITA_API_URL=$ALITA_API_URL
RUN npm run build

FROM node:20-alpine AS runner

WORKDIR /app
ARG ALITA_API_URL=http://alita-backend:8080
ENV ALITA_API_URL=$ALITA_API_URL

COPY --from=builder /app/package.json ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.js ./

EXPOSE 3000

CMD ["npm", "start"]
