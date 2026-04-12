FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .

# Cloud Run sets the PORT env var; default to 8080
ENV PORT=8080
EXPOSE 8080

# Start the server, respecting the PORT env var
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
