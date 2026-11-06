FROM python:3.11-slim

ARG DEBIAN_MIRROR=""
ARG PYPI_MIRROR=""
ARG PYPI_HOST=""

# Configure Debian APT mirror if specified
RUN if [ -n "${DEBIAN_MIRROR}" ]; then \
        echo "Configuring Debian APT mirror: ${DEBIAN_MIRROR}"; \
        mkdir -p /etc/apt/apt.conf.d; \
        echo 'Acquire::https::Verify-Peer "false";' > /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::https::Verify-Host "false";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::AllowInsecureRepositories "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'APT::Get::AllowUnauthenticated "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        . /etc/os-release; \
        CODENAME=${VERSION_CODENAME:-bookworm}; \
        rm -rf /etc/apt/sources.list.d/*; \
        echo "deb [trusted=yes] ${DEBIAN_MIRROR} ${CODENAME} main" > /etc/apt/sources.list; \
        echo "deb [trusted=yes] ${DEBIAN_MIRROR} ${CODENAME}-updates main" >> /etc/apt/sources.list; \
    fi

# Configure system UTF-8 locale and Python I/O encoding globally
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONIOENCODING=utf-8

# Install necessary build tools, system dependencies, and sandbox runtime prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    libicu-dev \
    && rm -rf /var/lib/apt/lists/*

# Configure Git globally to preserve non-ASCII / Persian UTF-8 filenames and commits
RUN git config --global core.quotepath false && \
    git config --global i18n.commitencoding utf-8 && \
    git config --global i18n.logoutputencoding utf-8

WORKDIR /app

# Create non-root system user for security (UID 1000 matches standard Linux primary user)
ARG USER_UID=1000
ARG USER_GID=1000
RUN (groupadd -g ${USER_GID} appgroup 2>/dev/null || groupadd appgroup) && \
    (useradd -u ${USER_UID} -g appgroup -s /bin/bash -m appuser 2>/dev/null || useradd -g appgroup -s /bin/bash -m appuser)

# Resilient pip configuration (timeouts, retries, and optional mirror)
RUN pip config set global.timeout 120 && \
    pip config set global.retries 10 && \
    if [ -n "${PYPI_MIRROR}" ]; then \
        pip config set global.index-url "${PYPI_MIRROR}"; \
    fi && \
    if [ -n "${PYPI_HOST}" ]; then \
        pip config set global.trusted-host "${PYPI_HOST}"; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

# Create persistent workspace directories for the non-root API user.  Bind-mount
# ownership is handled by the one-shot repo_permissions Compose service.
RUN mkdir -p /app/repos /app/data && chown -R appuser:appgroup /app && chmod -R u+rwX /app/repos /app/data

COPY --chown=appuser:appgroup . .

# Switch to non-root user
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
