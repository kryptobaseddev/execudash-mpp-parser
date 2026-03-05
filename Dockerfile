FROM python:3.11-slim

# Fix Debian slim's missing man-page directories.
# OpenJDK's post-install scripts (update-alternatives) try to create symlinks in
# /usr/share/man/man1 and fail silently (or noisily) when the directory is absent.
# Creating it beforehand ensures a clean JRE installation.
RUN mkdir -p /usr/share/man/man1 && \
    apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless && \
    rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME explicitly so JPype doesn't need subprocess detection to find the JVM.
# The RUN step validates the java binary exists; ENV sets it for all subsequent layers
# and runtime. Using the amd64 path — the base image is linux/amd64 on Railway.
RUN java -version 2>&1 && \
    JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java)))) && \
    echo "Detected JAVA_HOME: $JAVA_HOME"
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# IMPORTANT: Use shell form (not JSON array) so that ${PORT:-8000} is expanded
# by /bin/sh at runtime. Railway injects $PORT as an environment variable.
# JSON array form ["uvicorn", ..., "$PORT"] would pass the literal string "$PORT"
# to uvicorn, not the resolved port number.
#
# --workers 1 is mandatory: the JVM is a singleton per process; multiple workers
# would each attempt to start their own JVM and interfere with each other.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
