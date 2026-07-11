FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt gunicorn

COPY main.py ./
COPY app/ ./app/

EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:app"]