# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Prevent Python from writing bytecode and ensure unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Explicitly set and create the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libicu-dev curl && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/bin && curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp && chmod a+rx /app/bin/yt-dlp

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --no-compile --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make script executable
RUN chmod +x ./sldl
RUN chmod +x ./curl-wrapper.sh && mv ./curl-wrapper.sh /app/bin/curl

# needed for curl wrapper and yt-dlp
ENV PATH="/app/bin:${PATH}" 

# Create non-root user and set ownership
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 5000

# Run the application
CMD ["python", "SpotWebApp.py"]