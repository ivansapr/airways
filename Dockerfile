FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py tracking.py admin.py access.py flight_widget.py ./
COPY widgets ./widgets
COPY admin_static ./admin_static
RUN mkdir /data && chown 65534:65534 /data
USER 65534:65534
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV STATE_DB_PATH=/data/airways.sqlite3
ENV ADMIN_HOST=0.0.0.0 ADMIN_PORT=8080
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "bot.py"]
