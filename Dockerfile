FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/bot.py .

# State is persisted via a mounted volume at /data
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
