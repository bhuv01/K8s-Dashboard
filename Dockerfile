FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY status_server.py .

RUN useradd -m statususer && chown -R statususer:statususer /app
USER statususer

CMD ["python", "-u", "status_server.py"]
