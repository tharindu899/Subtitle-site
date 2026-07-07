FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7360

COPY requirements.txt ./
# PyroFork provides the compatible ``pyrogram`` import namespace. TgCrypto
# supplies the compiled MTProto acceleration layer for Telegram operations.
# Verify both during the image build so this deploy never installs the legacy
# Python Telegram client package by accident.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir --prefer-binary -r requirements.txt \
    && python -c "import importlib.metadata as m, pyrogram, tgcrypto; print(f'PyroFork {m.version(\"pyrofork\")} + TgCrypto {getattr(tgcrypto, \"__version__\", \"installed\")} ready')" \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY . ./

EXPOSE 7360
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7360} --proxy-headers --forwarded-allow-ips=*"]
