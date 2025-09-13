# Use slim Python base
FROM python:3.10-slim-buster

# Set working directory
WORKDIR /app

# Install system dependencies (needed by some doc loaders)
RUN apt-get update && apt-get install -y \
    build-essential libmagic1 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source code
COPY . .

# Expose container port
EXPOSE 5000

# Run with Gunicorn in production
# Assumes your Flask app instance is named `app` in app.py
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
