FROM node:20-bookworm

ARG OOBEE_REF=master
ENV CI=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    openjdk-17-jre-headless \
    zip \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone --depth 1 --branch ${OOBEE_REF} https://github.com/GovTechSG/purple-a11y.git . \
  && npm ci \
  && npx playwright install --with-deps chromium

ENTRYPOINT ["npm", "start", "--"]
