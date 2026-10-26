FROM acrcrvscus.azurecr.io/crv-research-agent@sha256:24a3d5e70b37c9e3176e5963c2238c140722ce5474e1b219a5b45289ae4db04f
USER 0
COPY crv_agent/ /tmp/crv-overlay/
RUN set -e; \
    for target in /app/crv_agent /usr/local/lib/python3.12/site-packages/crv_agent; do \
        cp -R /tmp/crv-overlay/. "$target"/; \
    done; \
    rm -rf /tmp/crv-overlay; \
    python -m compileall -q /app/crv_agent /usr/local/lib/python3.12/site-packages/crv_agent
USER 10001