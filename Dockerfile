# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set the working directory in the container
WORKDIR /app

# --- FIX 1: Force Python logs to show up immediately ---
ENV PYTHONUNBUFFERED=1

# Copy the requirements first to leverage Docker layer caching
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# --- FIX 3: Download NLTK Resources ---
# These are required by PineconeHybridSearch / BM25 for text tokenization
RUN python3 -m nltk.downloader punkt punkt_tab averaged_perceptron_tagger_eng

# Copy the rest of the application code
COPY . /app

# Run the model download script during the build process
RUN python3 download_model.py

# --- FIX 2: Update Command for Logs & Stability ---
# Note: You mentioned reducing workers to 1 in your comment, 
# but your CMD still had 4. I've set it to 2 as a stable middle ground for EC2.
# Change your existing gunicorn command to this:
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "run:app"]