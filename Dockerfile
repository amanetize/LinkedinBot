# Use the official Microsoft Playwright image with all dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# Set work directory
WORKDIR /app

# Set environments
ENV PYTHONUNBUFFERED=1

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Start the bot
CMD ["python", "bot.py"]
