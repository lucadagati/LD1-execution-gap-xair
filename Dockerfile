FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# The server loads schemas/ relative to the source tree, so run from /app.
COPY xair ./xair
COPY schemas ./schemas
ENV PYTHONPATH=/app
EXPOSE 8080
CMD ["uvicorn", "xair.adapters.http_server:app", "--host", "0.0.0.0", "--port", "8080"]
