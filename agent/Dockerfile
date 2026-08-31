# Agent Cloud Run deployment (optional checklist proof)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
# Cloud Run will set PORT env
ENV PORT=8080
CMD python web_ui.py --host 0.0.0.0 --port $PORT --no-browser
