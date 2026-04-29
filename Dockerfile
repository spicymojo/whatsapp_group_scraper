# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
# libmagic1 is required by neonize/python-magic
RUN apt-get update && apt-get install -y \
    libmagic1 \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements (since we don't have requirements.txt, we install manually)
RUN pip install --no-cache-dir neonize python-dotenv telethon

# Copy the rest of the application code
COPY . .

# Set environment variables
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUNBUFFERED=1

# Run the scraper
CMD ["python", "scraper.py"]
