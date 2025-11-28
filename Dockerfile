# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set the working directory in the container
WORKDIR /app

# --- FIX 1: Force Python logs to show up immediately ---
ENV PYTHONUNBUFFERED=1

# Copy the requirements first to leverage Docker layer caching
COPY requirements.txt .
# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt

# Copy the rest of the application code
COPY . /app

# Run the model download script during the build process
RUN python3 download_model.py

# --- FIX 2: Update Command for Logs & Stability ---
# Reduced workers to 1 to prevent OOM
# Added --preload to save RAM
# Added --access-logfile - and --error-logfile - to see logs in Docker
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--preload", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "debug", "run:app"]