FROM python:3.11-slim
WORKDIR /app
COPY server.py /app/
RUN pip install --no-cache-dir flask earthengine-api
# In production, don't copy the key file into the image; use Secret Manager or mount
# Copy entrypoint
CMD ["python", "server.py"]
