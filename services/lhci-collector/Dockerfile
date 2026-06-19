FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    dumb-init \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json /app/package.json
RUN npm install --omit=dev
COPY run-lhci.js /app/run-lhci.js
COPY scheduler.js /app/scheduler.js

CMD ["dumb-init", "node", "run-lhci.js"]
