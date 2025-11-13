# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements first to leverage Docker layer caching
COPY requirements.txt .
# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt

# Copy the rest of the application code
COPY . /app

# ⭐ ADDED STEP ⭐
# Run the model download script during the build process
RUN python3 download_model.py

# --- UPDATE THIS LINE ---
# Run gunicorn when the container launches, binding to the correct port
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "--workers", "4", "run:app"]