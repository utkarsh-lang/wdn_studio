FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Hugging Face Spaces runs containers as user 1000
RUN useradd -m -u 1000 app
WORKDIR /home/app/wdn-studio

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app . .
USER app

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
