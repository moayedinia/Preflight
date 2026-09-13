FROM dockerhub.ime.co.ir/python:3.11-slim

ARG DEBIAN_MIRROR="https://linux.repo.ime.co.ir/repository/debian"
ARG PYPI_MIRROR="https://pypi.repo.ime.co.ir/repository/pypi-group/simple/"
ARG PYPI_HOST="pypi.repo.ime.co.ir"

# Configure Debian APT mirror for internal/corporate network
RUN if [ -n "${DEBIAN_MIRROR}" ]; then \
        echo "Configuring internal Debian APT mirror: ${DEBIAN_MIRROR}"; \
        mkdir -p /etc/apt/apt.conf.d; \
        echo 'Acquire::https::Verify-Peer "false";' > /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::https::Verify-Host "false";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'Acquire::AllowInsecureRepositories "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        echo 'APT::Get::AllowUnauthenticated "true";' >> /etc/apt/apt.conf.d/99ssl-internal-mirror; \
        . /etc/os-release; \
        CODENAME=${VERSION_CODENAME:-trixie}; \
        rm -rf /etc/apt/sources.list.d/*; \
        echo "deb [trusted=yes] ${DEBIAN_MIRROR} ${CODENAME} main" > /etc/apt/sources.list; \
        echo "deb [trusted=yes] ${DEBIAN_MIRROR} ${CODENAME}-updates main" >> /etc/apt/sources.list; \
    fi

# Configure PyPI mirror globally for all pip operations
ENV PIP_INDEX_URL=${PYPI_MIRROR}
ENV PIP_TRUSTED_HOST=${PYPI_HOST}
ENV PIP_DEFAULT_TIMEOUT=180
ENV PIP_RETRIES=5

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
    pip config set global.trusted-host "${PYPI_HOST}" && \
    pip config set global.timeout "180" && \
    pip config set global.retries "5"

# Upgrade pip to latest version first
RUN pip install --no-cache-dir --default-timeout=180 --retries=5 --index-url "${PYPI_MIRROR}" --trusted-host "${PYPI_HOST}" --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=180 --retries=5 --index-url "${PYPI_MIRROR}" --trusted-host "${PYPI_HOST}" -r requirements.txt

# Create sandboxed repos directory and set ownership
RUN mkdir -p /app/repos && chown -R appuser:appgroup /app

COPY --chown=appuser:appgroup . .

# Switch to non-root user
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
