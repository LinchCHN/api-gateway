FROM python:3.11-alpine
RUN pip install --no-cache-dir flask httpx
WORKDIR /app
COPY app.py .
COPY templates/ templates/
EXPOSE 3000
CMD ["python", "app.py"]
