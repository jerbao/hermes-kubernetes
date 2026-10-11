ARG HERMES_BASE_IMAGE=hermes-personal-base:local
FROM ${HERMES_BASE_IMAGE}
USER root
# The upstream s6 compatibility shim is replaced by an actual subreaper: our
# Kubernetes containers run under an external supervisor as UID 10000.
ARG TINI_VERSION=0.19.0
ARG TINI_SHA256=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
RUN test "$(uname -m)" = x86_64 && \
    curl -fsSL --retry 3 -o /usr/bin/tini \
      "https://github.com/krallin/tini/releases/download/v${TINI_VERSION}/tini-static-amd64" && \
    printf '%s  /usr/bin/tini\n' "${TINI_SHA256}" | sha256sum -c - && \
    chmod 0755 /usr/bin/tini && /usr/bin/tini --version
# Native PM uses this baseline when its first writable environment replaces
# the image environment. Keep exactly the extras installed by the upstream build.
RUN /opt/hermes/.venv/bin/python -c "from pathlib import Path; from pm.features import write_features, declared_extras; extras=['all','messaging','otlp','anthropic','bedrock','azure-identity','matrix','google-chat']; assert set(extras) <= set(declared_extras(Path('/opt/hermes'))); write_features(extras, Path('/opt/hermes'))"
COPY source-provenance.json install-stamp.json /opt/hermes/
COPY hermes_kubernetes/ /opt/hermes-personal/hermes_kubernetes/
# Native launchers deliberately ignore PYTHONPATH (-I). Make the distribution
# importable from their explicit source root, including new PM generations.
RUN ln -s /opt/hermes-personal/hermes_kubernetes /opt/hermes/hermes_kubernetes
ENV PYTHONPATH=/opt/hermes-personal:/opt/hermes
COPY config/rclone.lock.json /opt/hermes-personal/rclone.lock.json
RUN /opt/hermes/.venv/bin/python /opt/hermes-personal/hermes_kubernetes/backup/tools.py \
    --lock /opt/hermes-personal/rclone.lock.json --destination /usr/local/bin/rclone
COPY skills/ /opt/hermes-personal/skills/
COPY config/ /opt/hermes-personal/config/
ENV HERMES_HOME=/opt/data
ENV HERMES_MANAGED_DIR=/etc/hermes-policy
ENV HOME=/opt/data
WORKDIR /opt/hermes
USER 10000:10000
ENTRYPOINT ["/usr/bin/tini", "-s", "-g", "--", "/opt/hermes/.venv/bin/python", "/opt/hermes-personal/hermes_kubernetes/runtime/bootstrap.py"]
CMD ["gateway"]
