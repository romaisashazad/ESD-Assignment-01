FROM python:3.12-slim
WORKDIR /app
COPY grpc-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt qrcode==7.4.2 Pillow==10.4.0
COPY common ./common
COPY grpc-service/server.py grpc-service/qr_pb2.py grpc-service/qr_pb2_grpc.py ./
ENV QR_DB_PATH=/data/qr.db
EXPOSE 50051
CMD ["python", "-u", "server.py"]