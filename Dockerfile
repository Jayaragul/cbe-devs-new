FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

RUN useradd --create-home --uid 10001 ai-signal \
    && chown -R ai-signal:ai-signal /app
USER ai-signal

EXPOSE 8080
CMD ["python", "ai_signal/live/local_server.py"]
