FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8443
# HTTPS direto no Uvicorn (TLS ponta-a-ponta até à réplica)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8443 --workers 1 --ssl-keyfile /certs/${NODE_ID}.key --ssl-certfile /certs/${NODE_ID}.crt"]
