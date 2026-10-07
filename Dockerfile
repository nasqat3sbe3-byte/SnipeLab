FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY risk_evidence.py .
COPY dashboard.html .
COPY low_float.py .
COPY low_float.html .
COPY low_float.js .
COPY history.py .
COPY rsi.py .
COPY storage.py .
COPY event_rules.py .
COPY corporate_actions.py .
COPY support_chart.py .
COPY opportunities.py .
COPY opportunities.html .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8080"]
