ARG DOCKER_REGISTRY=""
FROM ${DOCKER_REGISTRY}python:3.11-slim

ARG DEBIAN_MIRROR="https://linux.repo.ime.co.ir/repository/debian"
ARG PYPI_MIRROR="https://pypi.repo.ime.co.ir/repository/pypi-group/simple"
ARG PYPI_HOST="pypi.repo.ime.co.ir"

# Configure Debian APT mirror for internal/corporate network
RUN if [ -n "${DEBIAN_MIRROR}" ]; then \
        echo "Configuring internal Debian APT mirror: ${DEBIAN_MIRROR}"; \
        mkdir -p /etc/apt/apt.conf.d; \
        echo 'Acquire::https::Verify-Peer "false";' > /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::https::Verify-Host "false";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::AllowInsecureRepositories "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'APT::Get::AllowUnauthenticated "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i -E "s|https?://deb.debian.org/debian-security/?|${DEBIAN_MIRROR}/|g" /etc/apt/sources.list; \
            sed -i -E "s|https?://security.debian.org/debian-security/?|${DEBIAN_MIRROR}/|g" /etc/apt/sources.list; \
            sed -i -E "s|https?://deb.debian.org/debian/?|${DEBIAN_MIRROR}/|g" /etc/apt/sources.list; \
        fi; \
        if [ -d /etc/apt/sources.list.d ]; then \
            for f in /etc/apt/sources.list.d/*; do \
                [ -e "$f" ] || continue; \
                sed -i -E "s|https?://deb.debian.org/debian-security/?|${DEBIAN_MIRROR}/|g" "$f"; \
                sed -i -E "s|https?://security.debian.org/debian-security/?|${DEBIAN_MIRROR}/|g" "$f"; \
                sed -i -E "s|https?://deb.debian.org/debian/?|${DEBIAN_MIRROR}/|g" "$f"; \
            done; \
        fi; \
    fi

# Configure PyPI mirror globally for all pip operations
ENV PIP_INDEX_URL=${PYPI_MIRROR}
ENV PIP_TRUSTED_HOST=${PYPI_HOST}

# Install necessary build tools and security dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create non-root system user for security
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/bash -m appuser

# Configure pip config file for system-wide package installations
RUN pip config set global.index-url "${PYPI_MIRROR}" && \
    pip config set global.trusted-host "${PYPI_HOST}"

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PYPI_MIRROR}" --trusted-host "${PYPI_HOST}" -r requirements.txt

# Create sandboxed repos directory and set ownership
RUN mkdir -p /app/repos && chown -R appuser:appgroup /app

COPY --chown=appuser:appgroup . .

# Switch to non-root user
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
