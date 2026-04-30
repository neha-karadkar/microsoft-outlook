# Emerson Sales Quote Builder Agent

Automates the creation, validation, approval, and delivery of sales quotations in response to customer RFQs received via email. Integrates with Outlook (Microsoft Graph), ERP, and compliance systems, orchestrating LLM-driven workflows with robust audit logging, observability, and content safety guardrails.

---

## Quick Start

### 1. Create a virtual environment:
```
python -m venv .venv
```

### 2. Activate the virtual environment:
- **Windows:**
  ```
  .venv\Scripts\activate
  ```
- **macOS/Linux:**
  ```
  source .venv/bin/activate
  ```

### 3. Install dependencies:
```
pip install -r requirements.txt
```

### 4. Environment setup:
Copy `.env.example` to `.env` and fill in all required values.
```
cp .env.example .env
```

### 5. Running the agent

- **Direct execution:**
  ```
  python code/agent.py
  ```
- **As a FastAPI server:**
  ```
  uvicorn code.agent:app --reload --host 0.0.0.0 --port 8000
  ```

---

## Environment Variables

**Agent Identity**
- `AGENT_NAME`
- `AGENT_ID`
- `PROJECT_NAME`
- `PROJECT_ID`
- `SERVICE_NAME`
- `SERVICE_VERSION`
- `VERSION`

**General**
- `ENVIRONMENT`

**Azure Key Vault (optional for production)**
- `USE_KEY_VAULT`
- `KEY_VAULT_URI`
- `AZURE_USE_DEFAULT_CREDENTIAL`
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`

**LLM Configuration**
- `MODEL_PROVIDER`
- `LLM_MODEL`
- `LLM_TEMPERATURE`
- `LLM_MAX_TOKENS`
- `OPENAI_API_KEY`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `LLM_MODELS` (JSON list, for cost calculation)

**Content Safety**
- `AZURE_CONTENT_SAFETY_ENDPOINT`
- `AZURE_CONTENT_SAFETY_KEY`
- `CONTENT_SAFETY_ENABLED`
- `CONTENT_SAFETY_SEVERITY_THRESHOLD`

**Observability Database (Azure SQL)**
- `OBS_DATABASE_TYPE`
- `OBS_AZURE_SQL_SERVER`
- `OBS_AZURE_SQL_DATABASE`
- `OBS_AZURE_SQL_PORT`
- `OBS_AZURE_SQL_USERNAME`
- `OBS_AZURE_SQL_PASSWORD`
- `OBS_AZURE_SQL_SCHEMA`
- `OBS_AZURE_SQL_TRUST_SERVER_CERTIFICATE`

**Tool Integration**
- `GRAPH_TENANT_ID`
- `GRAPH_CLIENT_ID`
- `GRAPH_CLIENT_SECRET`
- `ERP_API_KEY`
- `COMPLIANCE_API_KEY`

**Service Endpoints (optional for RAG)**
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_API_KEY`
- `AZURE_SEARCH_INDEX_NAME`

**Agent-Specific**
- `VALIDATION_CONFIG_PATH`

See `.env.example` for all required and optional variables.

---

## API Endpoints

### **GET** `/health`
Health check endpoint.

**Response:**
```
{
  "status": "ok"
}
```

---

### **POST** `/query`
Process a user query (RFQ intake, quote generation, etc).

**Request body:**
```
{
  "query": "string (required)",
  "attachments": ["string"] (optional)
}
```

**Response:**
```
{
  "success": true|false,
  "content": "string|null",
  "error": "string|null",
  "tool_calls_made": ["string"]|null
}
```

---

## Running Tests

### 1. Install test dependencies (if not already installed):
```
pip install pytest pytest-asyncio
```

### 2. Run all tests:
```
pytest tests/
```

### 3. Run a specific test file:
```
pytest tests/test_<module_name>.py
```

### 4. Run tests with verbose output:
```
pytest tests/ -v
```

### 5. Run tests with coverage report:
```
pip install pytest-cov
pytest tests/ --cov=code --cov-report=term-missing
```

---

## Deployment with Docker

### 1. Prerequisites: Ensure Docker is installed and running.

### 2. Environment setup: Copy `.env.example` to `.env` and configure all required environment variables.

### 3. Build the Docker image:
```
docker build -t Emerson-Sales-Quote-Builder-Agent -f deploy/Dockerfile .
```

### 4. Run the Docker container:
```
docker run -d --env-file .env -p 8000:8000 --name Emerson-Sales-Quote-Builder-Agent Emerson-Sales-Quote-Builder-Agent
```

### 5. Verify the container is running:
```
docker ps
```

### 6. View container logs:
```
docker logs Emerson-Sales-Quote-Builder-Agent
```

### 7. Stop the container:
```
docker stop Emerson-Sales-Quote-Builder-Agent
```

---

## Notes

- All run commands must use the `code/` prefix (e.g., `python code/agent.py`, `uvicorn code.agent:app ...`).
- See `.env.example` for all required and optional environment variables.
- The agent requires access to LLM API keys and (optionally) Azure SQL for observability.
- For production, configure Key Vault and secure credentials as needed.

---

**Emerson Sales Quote Builder Agent** — Automate, validate, and deliver compliant sales quotes from RFQs with LLM-powered orchestration.