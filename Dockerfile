# dba-observer — two images from one recipe:
#
#   docker build --target toolbox -t dba-observer-toolbox:<version> .
#   docker build --target adapter -t dba-observer-adapter:<version> .
#
# Both tags use the version in pyproject.toml; build them from the matching
# git tag (v<version>) only.

# -----------------------------------------------------------------------------
# Toolbox: the upstream binary, lifted off its distroless image onto a base
# with a shell, so the entrypoint can source the mounted secret file (secrets
# are files, never compose `environment:`). The binary is glibc-linked, so
# Debian rather than Alpine.
# -----------------------------------------------------------------------------
FROM us-central1-docker.pkg.dev/database-toolbox/toolbox/toolbox:1.14.0@sha256:dab91e25cbe7145d261df2978881c4e23e02238b42da3c72eea2df0a95aea797 AS upstream-toolbox

FROM debian:12-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS toolbox
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1607 dbaobs && useradd -u 1607 -g 1607 -M -s /usr/sbin/nologin dbaobs
COPY --from=upstream-toolbox /toolbox /usr/local/bin/toolbox
COPY docker/tools.yaml /etc/dba-observer/tools.yaml
COPY docker/toolbox-entrypoint.sh /usr/local/bin/entrypoint
COPY docker/check-tools-yaml.sh /usr/local/bin/check-tools-yaml
RUN chmod 0755 /usr/local/bin/toolbox /usr/local/bin/entrypoint /usr/local/bin/check-tools-yaml \
 && chmod 0444 /etc/dba-observer/tools.yaml \
 && /usr/local/bin/check-tools-yaml /etc/dba-observer/tools.yaml
USER 1607:1607
EXPOSE 5000
# No curl in the image: a plain HTTP GET over bash's /dev/tcp.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/5000 && printf 'GET / HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3 && head -1 <&3 | grep -q ' 200 '"]
ENTRYPOINT ["/usr/local/bin/entrypoint"]

# -----------------------------------------------------------------------------
# Adapter: Redis tools, PostgreSQL per-database catalog tools, meta tools.
# Reachable only from the Toolbox container, on an internal network.
# Every third-party package, build backend included, comes from a hash-pinned
# lock file, so rebuilding a tag reproduces the dependencies the tests ran on.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS adapter-wheel
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY docker/build-requirements.lock ./
RUN pip install --require-hashes -r build-requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-deps --no-build-isolation -w /wheels .

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS adapter
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY docker/requirements.lock /tmp/requirements.lock
COPY --from=adapter-wheel /wheels /tmp/wheels
RUN pip install --require-hashes -r /tmp/requirements.lock \
 && pip install --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/requirements.lock /tmp/wheels \
 && groupadd -g 1608 dbaobs && useradd -u 1608 -g 1608 -M -s /usr/sbin/nologin dbaobs
COPY docker/adapter-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod 0755 /usr/local/bin/entrypoint
USER 1608:1608
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
ENTRYPOINT ["/usr/local/bin/entrypoint"]
