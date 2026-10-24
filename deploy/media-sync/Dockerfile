# One-shot uploader of the site media into SeaweedFS (docker-compose.yml: media-sync). The script itself is mounted from
# scripts/media_sync.py, so changing it needs no rebuild; this image only carries Python + boto3.
FROM python:3.12-slim
RUN pip install --no-cache-dir boto3==1.40.1
WORKDIR /app
ENTRYPOINT ["python", "/app/media_sync.py"]
