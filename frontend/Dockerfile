# The UI: built with Node, served as static files by nginx.
#
# The build stage is the only part of CrowdSight that needs the internet, and
# it needs it at *build* time only — the same category as pulling a base image
# or the qwen2.5 model during provisioning. `npm ci` installs exactly what
# package-lock.json pins, so the tree is reproducible and auditable.
#
# The runtime stage carries the compiled bundle and nothing else: no Node, no
# node_modules, no package manager. It joins the sealed network, where there is
# nowhere to fetch from anyway.

FROM node:22-alpine AS build
WORKDIR /build

# Dependencies first, so a change to the source does not reinstall them.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80

# 127.0.0.1 rather than localhost: the container resolves localhost to ::1
# first, and nginx binds IPv4 only, so the probe would fail against a server
# that is serving perfectly well.
HEALTHCHECK --interval=20s --timeout=5s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/_frontend/health >/dev/null 2>&1 || exit 1
