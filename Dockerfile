FROM python:3.12-slim
WORKDIR /app
RUN mkdir /data && chown 65534:65534 /data
COPY app.py .
USER 65534:65534
EXPOSE 8081
CMD ["python", "-u", "app.py"]
