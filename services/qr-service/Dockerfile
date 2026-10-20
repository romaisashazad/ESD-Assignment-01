FROM python:3.12-slim
WORKDIR /app
COPY qr-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY qr-service/app.py .
ENV QR_DB_PATH=/data/qr.db
EXPOSE 5000
CMD ["python", "app.py"]