FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY agentic_turn ./agentic_turn
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --create-home demo && mkdir data && chown demo data
USER demo
EXPOSE 8000
CMD ["uvicorn", "agentic_turn.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
