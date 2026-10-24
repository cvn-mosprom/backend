# One-shot uploader of the site media into SeaweedFS (docker-compose.yml: media-sync). The script itself is mounted from
# scripts/media_sync.py, so changing it needs no rebuild; this image only carries Python + boto3.
FROM docker.io/library/python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d
RUN pip install --no-cache-dir boto3==1.40.1
WORKDIR /app
ENTRYPOINT ["python", "/app/media_sync.py"]
