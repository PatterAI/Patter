FROM python:3.13-slim

WORKDIR /app

# Install Patter SDK with local mode dependencies
RUN pip install --no-cache-dir "getpatter[local]" python-dotenv

# Copy your agent script and .env
COPY . .

EXPOSE 8000

# Default: run the Python example at python/main.py
# Override with: docker run patter python your_script.py
CMD ["python", "python/main.py"]
