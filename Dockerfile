FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-core libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sereno ./sereno
COPY scripts ./scripts
RUN pip install --no-cache-dir .
RUN useradd -m sereno && mkdir -p /data && chown sereno /data
USER sereno
ENV SERENO_DATA_DIR=/data SERENO_ENV=prod
EXPOSE 8000
CMD ["uvicorn", "sereno.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
