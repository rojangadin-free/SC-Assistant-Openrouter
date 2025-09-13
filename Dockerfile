# Use latest stable Python 3.11 on Debian Bookworm (Debian 12)
FROM python:3.11-slim-bookworm

# Set working directory inside container
WORKDIR /app

# Copy project files
COPY . /app

# Install system dependencies (needed for some Python packages / document loaders)
RUN apt-get update && apt-get install -y \
    build-essential \
    libmagic1 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port (use 5000 if Flask default, change if you mapped differently in docker run)
EXPOSE 5000

# Start the application
CMD ["python", "app.py"]
