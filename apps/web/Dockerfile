FROM node:20-alpine

WORKDIR /app

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./

ARG LEVEL_API_PROXY_TARGET=https://level-api-185318998255.us-central1.run.app
ENV LEVEL_API_PROXY_TARGET=$LEVEL_API_PROXY_TARGET

RUN npm run build

ENV PORT=8080
ENV HOSTNAME=0.0.0.0
EXPOSE 8080

CMD ["npx", "next", "start", "--port", "8080", "--hostname", "0.0.0.0"]
