ARG NODE_IMAGE=node:24.21.0-alpine3.24@sha256:83f1c388c31fb2e51f7cbd4dea949b96260798c98f206e8e4696bc93bd964e3a
ARG NODE_GLIBC_IMAGE=node:24.21.0-bookworm-slim@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd
ARG ALMA_IMAGE=docker.io/almalinux/10-minimal@sha256:f331feabbc2a05575695fc1a384d0bc002a46a24ef4f9e5d65ff7085e1494074
FROM ${NODE_GLIBC_IMAGE} AS node-glibc
FROM ${NODE_IMAGE} AS alpine
RUN apk add --no-cache ca-certificates python3 py3-pip openssh-client tini git \
    && python3 -m venv /opt/mcp-venv \
    && addgroup -g 10000 -S mcp \
    && adduser -u 10000 -S -D -H -h /var/lib/mcp -G mcp mcp
COPY dependencies/mcp/requirements.txt /opt/dependencies/mcp/requirements.txt
RUN /opt/mcp-venv/bin/pip install --no-cache-dir -r /opt/dependencies/mcp/requirements.txt
COPY dependencies/mcp/package.json dependencies/mcp/package-lock.json /opt/mcp/
RUN cd /opt/mcp && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --omit=dev \
    && npm cache clean --force
COPY hermes_kubernetes/ /opt/mcp/hermes_kubernetes/
COPY dependencies/mcp/ /opt/mcp/dependencies/mcp/
USER 10000:10000
ENV HOME=/var/lib/mcp
ENV PYTHONPATH=/opt/mcp
ENV PATH=/opt/mcp/node_modules/.bin:/opt/mcp-venv/bin:$PATH
WORKDIR /var/lib/mcp
EXPOSE 2222
ENTRYPOINT ["/sbin/tini", "-s", "-g", "--", "/opt/mcp-venv/bin/python", "/opt/mcp/hermes_kubernetes/executors/runner.py"]

# Codegraph ships a glibc-only Node executable and native kernel. Its executor
# uses the same AlmaLinux x86-64-v2 base validated for Hermes; other executors
# retain Alpine. Node and package versions remain identical across variants.
FROM ${ALMA_IMAGE} AS codegraph
RUN microdnf install -y --setopt=install_weak_deps=0 \
      ca-certificates python3 python3-pip openssh-clients git curl libstdc++ libatomic shadow-utils procps-ng \
      binutils gdb wireshark-cli e2fsprogs cpio util-linux \
    && microdnf clean all \
    && python3 -m venv /opt/mcp-venv \
    && groupadd -g 10000 mcp \
    && useradd -u 10000 -g 10000 -M -d /var/lib/mcp mcp \
    && groupadd -g 1000 workspace \
    && useradd -u 1000 -g 1000 -M -d /var/lib/mcp workspace
COPY --from=node-glibc /usr/local/bin/node /usr/local/bin/node
COPY --from=node-glibc /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ARG TINI_VERSION=0.19.0
ARG TINI_SHA256=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
RUN curl -fsSL --retry 3 -o /sbin/tini \
      "https://github.com/krallin/tini/releases/download/v${TINI_VERSION}/tini-static-amd64" \
    && printf '%s  /sbin/tini\n' "${TINI_SHA256}" | sha256sum -c - \
    && chmod 0755 /sbin/tini
COPY dependencies/mcp/requirements.txt /opt/dependencies/mcp/requirements.txt
RUN /opt/mcp-venv/bin/pip install --no-cache-dir -r /opt/dependencies/mcp/requirements.txt
COPY --from=alpine /opt/mcp /opt/mcp
USER 10000:10000
ENV HOME=/var/lib/mcp
ENV PYTHONPATH=/opt/mcp
ENV PATH=/opt/mcp/node_modules/.bin:/opt/mcp-venv/bin:$PATH
WORKDIR /var/lib/mcp
EXPOSE 2222
ENTRYPOINT ["/sbin/tini", "-s", "-g", "--", "/opt/mcp-venv/bin/python", "/opt/mcp/hermes_kubernetes/executors/runner.py"]

FROM alpine AS runtime
