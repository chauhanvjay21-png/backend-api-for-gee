FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /app/server.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Install python packages
RUN pip install --no-cache-dir flask earthengine-api

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "server.py"]
