# An image for the visual check of the interface: a screenshot + measurements of the invariants.
#
# Why a separate image: the browser dependencies do not need to (and cannot) be installed on the VPS
# (the owner has no root, the production host has its own role). Everything lives in a container,
# which is rebuilt from this file in minutes.
#
# ‼️ The image was lost once already: it had been built by hand, was described nowhere, and
# after a docker cleanup the visual check became impossible — the audit of 02.08.2026
# went through without a single screenshot. That is why the Dockerfile lives in the repository.
#
# Build:   docker build -t shots:local scripts/shots
# Run:   scripts/shots/shot.sh --url https://... --out /out/page.png
FROM node:22-bookworm-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_ENV=production

WORKDIR /work

COPY package.json ./
RUN npm install --omit=dev \
    && npx playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY shot.js ./

ENTRYPOINT ["node", "shot.js"]
