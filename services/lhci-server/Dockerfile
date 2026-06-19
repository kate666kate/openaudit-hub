FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends dumb-init \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json /app/package.json
RUN npm install --omit=dev
COPY server.js /app/server.js

EXPOSE 9001
CMD ["dumb-init", "node", "server.js"]
