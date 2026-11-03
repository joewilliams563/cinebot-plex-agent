FROM python:3.11-slim

WORKDIR /workspace
ENV PYTHONPATH=/workspace

# Install system dependencies & curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000 8501

# Launch FastAPI webhook backend in the background and Chainlit UI in the foreground
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 & chainlit run ui/chat_app.py --host 0.0.0.0 --port 8501"]