FROM python:3.12-slim

# Install Node.js 20.x and git/curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Node dependencies
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY bridge.js contact_engine.py app.py ./
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 5050

CMD ["python3", "app.py"]
