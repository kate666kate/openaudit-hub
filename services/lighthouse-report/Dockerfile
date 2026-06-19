FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    dumb-init \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json /app/package.json
RUN npm install --omit=dev
COPY run-lighthouse-report.js /app/run-lighthouse-report.js

CMD ["dumb-init", "node", "run-lighthouse-report.js"]
