# The browser client: built with Node, served by nginx.
#
# The build stage is discarded. A production image containing node_modules
# ships a package manager, a compiler toolchain, and several hundred megabytes
# of development dependencies to serve files that were finished at build time.
#
# nginx also serves as the reverse proxy to the API, which is the same decision
# the Vite dev server makes: one origin in the browser. That is not laziness --
# it means CORS is never load-bearing, cookies behave the same in development
# and production, and the WebSocket goes to the page's own origin rather than a
# second host that has to be separately allowed.

FROM node:22-slim AS build

WORKDIR /build

# Lockfile first, for the same layer-caching reason as the Python image.
COPY package.json package-lock.json* ./
# `npm ci` rather than `npm install`: it installs exactly the lockfile and fails
# if package.json disagrees with it, so a build cannot quietly resolve a
# different dependency tree than the one that was tested.
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# nginx's own image already drops to an unprivileged user for the workers, but
# the master runs as root to bind port 80. Binding 8080 instead lets the whole
# thing run unprivileged.
EXPOSE 8080
