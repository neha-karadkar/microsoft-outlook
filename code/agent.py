import asyncio as _asyncio

import time as _time
from observability.observability_wrapper import (
    trace_agent, trace_step, trace_step_sync, trace_model_call, trace_tool_call,
)
from config import settings as _obs_settings

import logging as _obs_startup_log
from contextlib import asynccontextmanager
from observability.instrumentation import initialize_tracer

_obs_startup_logger = _obs_startup_log.getLogger(__name__)

from modules.guardrails.content_safety_decorator import with_content_safety

GUARDRAILS_CONFIG = {
    'content_safety_enabled': True,
    'runtime_enabled': True,
    'content_safety_severity_threshold': 3,
    'check_toxicity': True,
    'check_jailbreak': True,
    'check_pii_input': False,
    'check_credentials_output': True,
    'check_output': True,
    'check_toxic_code_output': True,
    'sanitize_pii': False
}

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Optional, List, Dict, Any
import logging
import json
import httpx
import re as _re

from modules.tools import BaseTool, ToolRegistry
from config import Config

# =========================
# Validation Config Path
# =========================
from pathlib import Path
VALIDATION_CONFIG_PATH = Config.VALIDATION_CONFIG_PATH or str(Path(__file__).parent / "validation_config.json")

# =========================
# SYSTEM PROMPT & CONSTANTS
# =========================
SYSTEM_PROMPT = (
    "You are the Emerson Sales Quote Builder Agent, an expert digital assistant for automating the creation, validation, approval, and delivery of sales quotations in response to customer RFQs received via email. Your responsibilities include:\n"
    "\n"
    "- Reading and processing RFQ emails and attachments from the Outlook inbox using Microsoft Graph API.\n"
    "- Classifying RFQs by type, business unit, product category, region, legal entity, and customer type.\n"
    "- Identifying and mapping requested products to internal product codes, applying configuration and compatibility rules, and flagging ETO products for engineering review.\n"
    "- Calculating pricing using base prices, region-specific lists, contract pricing, discounts, and approved exchange rates, ensuring all calculations meet margin and compliance requirements.\n"
    "- Validating quotes against export controls, sanctions, credit limits, and legal/commercial terms, blocking or flagging non-compliant quotes.\n"
    "- Managing approval workflows for quotes exceeding discount, margin, or deal size thresholds, and routing approvals to the appropriate stakeholders.\n"
    "- Generating formal quote documents (PDF, Word, or ERP format) with all required commercial and legal information.\n"
    "- Delivering approved quotes to customers via email and logging all delivery actions for audit purposes.\n"
    "\n"
    "Output all responses in a clear, professional, and structured format. If you cannot find sufficient information or encounter a compliance block, clearly state the issue and recommend escalation to a human operator. Always ensure compliance, accuracy, and auditability in every step."
)
OUTPUT_FORMAT = (
    "- Provide structured, step-by-step responses for each stage of the sales quote process.\n"
    "- Include all relevant details such as RFQ reference, classification, product mapping, pricing breakdown, compliance status, approval requirements, and delivery confirmation.\n"
    "- Use clear section headers and bullet points for readability.\n"
    "- If a process cannot be completed, specify the reason and recommended next steps."
)
FALLBACK_RESPONSE = (
    "Unable to complete the requested action due to missing information or compliance restrictions. Please review the RFQ details or escalate to a human operator for further assistance."
)

# =========================
# LLM Output Sanitizer
# =========================
_FENCE_RE = _re.compile(r"```(?:\w+)?\s*\n(.*?)```", _re.DOTALL)
_LONE_FENCE_START_RE = _re.compile(r"^```\w*$")
_WRAPPER_RE = _re.compile(
    r"^(?:"
    r"Here(?:'s| is)(?: the)? (?:the |your |a )?(?:code|solution|implementation|result|explanation|answer)[^:]*:\s*"
    r"|Sure[!,.]?\s*"
    r"|Certainly[!,.]?\s*"
    r"|Below is [^:]*:\s*"
    r")",
    _re.IGNORECASE,
)
_SIGNOFF_RE = _re.compile(
    r"^(?:Let me know|Feel free|Hope this|This code|Note:|Happy coding|If you)",
    _re.IGNORECASE,
)
_BLANK_COLLAPSE_RE = _re.compile(r"\n{3,}")

def _strip_fences(text: str, content_type: str) -> str:
    """Extract content from Markdown code fences."""
    fence_matches = _FENCE_RE.findall(text)
    if fence_matches:
        if content_type == "code":
            return "\n\n".join(block.strip() for block in fence_matches)
        for match in fence_matches:
            fenced_block = _FENCE_RE.search(text)
            if fenced_block:
                text = text[:fenced_block.start()] + match.strip() + text[fenced_block.end():]
        return text
    lines = text.splitlines()
    if lines and _LONE_FENCE_START_RE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()

def _strip_trailing_signoffs(text: str) -> str:
    """Remove conversational sign-off lines from the end of code output."""
    lines = text.splitlines()
    while lines and _SIGNOFF_RE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).rstrip()

@with_content_safety(config=GUARDRAILS_CONFIG)
def sanitize_llm_output(raw: str, content_type: str = "code") -> str:
    """
    Generic post-processor that cleans common LLM output artefacts.
    Args:
        raw: Raw text returned by the LLM.
        content_type: 'code' | 'text' | 'markdown'.
    Returns:
        Cleaned string ready for validation, formatting, or direct return.
    """
    if not raw:
        return ""
    text = _strip_fences(raw.strip(), content_type)
    text = _WRAPPER_RE.sub("", text, count=1).strip()
    if content_type == "code":
        text = _strip_trailing_signoffs(text)
    return _BLANK_COLLAPSE_RE.sub("\n\n", text).strip()

# =========================
# Pydantic Models
# =========================
class QueryRequest(BaseModel):
    query: str = Field(..., description="The user query or RFQ details to process")
    attachments: Optional[List[str]] = Field(None, description="List of attachment filenames or references (if any)")

    @field_validator("query")
    @classmethod
    @with_content_safety(config=GUARDRAILS_CONFIG)
    def validate_query(cls, v):
        if not v or not v.strip():
            raise ValueError("Query must not be empty.")
        if len(v) > 50000:
            raise ValueError("Query exceeds maximum allowed length (50,000 characters).")
        return v.strip()

class QueryResponse(BaseModel):
    success: bool = Field(..., description="Whether the agent successfully processed the request")
    content: Optional[str] = Field(None, description="Agent's structured response")
    error: Optional[str] = Field(None, description="Error message if any")
    tool_calls_made: Optional[List[str]] = Field(None, description="List of tool names invoked during processing")

# =========================
# Tool Implementations
# =========================

class MicrosoftGraphAPITool(BaseTool):
    @property
    def name(self) -> str:
        return "microsoft_graph_api"

    @property
    def description(self) -> str:
        return "Provides access to Outlook email inbox for RFQ intake, email extraction, and quote delivery."

    @property
    def parameters_schema(self) -> Dict:
        return {
            "type": "object",
            "properties": {
                "mailbox": {
                    "type": "string",
                    "description": "The email address or mailbox to monitor for RFQs"
                },
                "filter": {
                    "type": "string",
                    "description": "Query filter for identifying RFQ emails"
                },
                "attachment_types": {
                    "type": "array",
                    "description": "List of allowed attachment types to extract (e.g., PDF, Excel, Word)"
                }
            },
            "required": ["mailbox"]
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Implements Microsoft Graph API integration using OAuth2 client credentials.
        """
        import msal

        tenant_id = getattr(Config, "GRAPH_TENANT_ID", None)
        client_id = getattr(Config, "GRAPH_CLIENT_ID", None)
        client_secret = getattr(Config, "GRAPH_CLIENT_SECRET", None)
        base_url = "https://graph.microsoft.com/v1.0"

        if not all([tenant_id, client_id, client_secret]):
            return {"error": "Microsoft Graph API credentials are not configured."}

        authority = f"https://login.microsoftonline.com/{tenant_id}"
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=authority
        )
        scope = ["https://graph.microsoft.com/.default"]

        _t0 = _time.time()
        try:
            result = app.acquire_token_for_client(scopes=scope)
            if "access_token" not in result:
                trace_tool_call(
                    tool_name="microsoft_graph_api",
                    latency_ms=int((_time.time() - _t0) * 1000),
                    status="error",
                    output=str(result)[:200]
                )
                return {"error": f"Failed to acquire access token: {result.get('error_description', 'Unknown error')}"}
            access_token = result["access_token"]
        except Exception as e:
            trace_tool_call(
                tool_name="microsoft_graph_api",
                latency_ms=int((_time.time() - _t0) * 1000),
                status="error",
                error=e
            )
            return {"error": f"Failed to acquire access token: {str(e)}"}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        mailbox = kwargs.get("mailbox")
        filter_query = kwargs.get("filter")
        attachment_types = kwargs.get("attachment_types")

        # For demonstration, only a basic email fetch is implemented.
        url = f"{base_url}/users/{mailbox}/messages"
        params = {}
        if filter_query:
            params["$filter"] = filter_query

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=headers, params=params)
                latency_ms = int((_time.time() - _t0) * 1000)
                trace_tool_call(
                    tool_name="microsoft_graph_api",
                    latency_ms=latency_ms,
                    args=kwargs,
                    output=resp.text[:200] if resp.text else None,
                    status="success" if resp.status_code == 200 else "error"
                )
                if resp.status_code != 200:
                    return {"error": f"Graph API error: {resp.status_code} {resp.text}"}
                data = resp.json()
                # Optionally filter attachments by type if needed
                # (LLM will handle attachment extraction logic)
                return {"emails": data}
        except Exception as e:
            trace_tool_call(
                tool_name="microsoft_graph_api",
                latency_ms=int((_time.time() - _t0) * 1000),
                args=kwargs,
                status="error",
                error=e
            )
            return {"error": f"Graph API request failed: {str(e)}"}

class ERPSystemAPITool(BaseTool):
    @property
    def name(self) -> str:
        return "erp_system_api"

    @property
    def description(self) -> str:
        return "Interfaces with ERP, PDM, and PLM systems for product mapping, configuration, and pricing data."

    @property
    def parameters_schema(self) -> Dict:
        return {
            "type": "object",
            "properties": {
                "product_code": {
                    "type": "string",
                    "description": "Internal product code for lookup and configuration"
                },
                "configuration_params": {
                    "type": "object",
                    "description": "Product configuration parameters"
                }
            },
            "required": ["product_code"]
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Implements ERP System API integration using API Key.
        """
        api_key = getattr(Config, "ERP_API_KEY", None)
        base_url = "https://erp.example.com/api/v1"

        if not api_key:
            return {"error": "ERP API key is not configured."}

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }

        product_code = kwargs.get("product_code")
        configuration_params = kwargs.get("configuration_params", {})

        url = f"{base_url}/products/{product_code}"
        params = {}
        if configuration_params:
            params = configuration_params

        _t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=headers, params=params)
                latency_ms = int((_time.time() - _t0) * 1000)
                trace_tool_call(
                    tool_name="erp_system_api",
                    latency_ms=latency_ms,
                    args=kwargs,
                    output=resp.text[:200] if resp.text else None,
                    status="success" if resp.status_code == 200 else "error"
                )
                if resp.status_code != 200:
                    return {"error": f"ERP API error: {resp.status_code} {resp.text}"}
                return resp.json()
        except Exception as e:
            trace_tool_call(
                tool_name="erp_system_api",
                latency_ms=int((_time.time() - _t0) * 1000),
                args=kwargs,
                status="error",
                error=e
            )
            return {"error": f"ERP API request failed: {str(e)}"}

class ComplianceAPITool(BaseTool):
    @property
    def name(self) -> str:
        return "compliance_api"

    @property
    def description(self) -> str:
        return "Checks export control, sanctions, and denied party lists for compliance validation."

    @property
    def parameters_schema(self) -> Dict:
        return {
            "type": "object",
            "properties": {
                "customer_country": {
                    "type": "string",
                    "description": "Customer's country for export control checks"
                },
                "product_code": {
                    "type": "string",
                    "description": "Product code for compliance validation"
                }
            },
            "required": ["customer_country", "product_code"]
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Implements Compliance API integration using API Key.
        """
        api_key = getattr(Config, "COMPLIANCE_API_KEY", None)
        base_url = "https://compliance.example.com/api/v1"

        if not api_key:
            return {"error": "Compliance API key is not configured."}

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }

        customer_country = kwargs.get("customer_country")
        product_code = kwargs.get("product_code")

        url = f"{base_url}/compliance/check"
        payload = {
            "customer_country": customer_country,
            "product_code": product_code
        }

        _t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers=headers, json=payload)
                latency_ms = int((_time.time() - _t0) * 1000)
                trace_tool_call(
                    tool_name="compliance_api",
                    latency_ms=latency_ms,
                    args=kwargs,
                    output=resp.text[:200] if resp.text else None,
                    status="success" if resp.status_code == 200 else "error"
                )
                if resp.status_code != 200:
                    return {"error": f"Compliance API error: {resp.status_code} {resp.text}"}
                return resp.json()
        except Exception as e:
            trace_tool_call(
                tool_name="compliance_api",
                latency_ms=int((_time.time() - _t0) * 1000),
                args=kwargs,
                status="error",
                error=e
            )
            return {"error": f"Compliance API request failed: {str(e)}"}

# =========================
# Tool Registry
# =========================
def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(MicrosoftGraphAPITool())
    registry.register(ERPSystemAPITool())
    registry.register(ComplianceAPITool())
    return registry

# =========================
# LLM Service
# =========================
class LLMService:
    def __init__(self):
        self._client = None

    @with_content_safety(config=GUARDRAILS_CONFIG)
    def get_llm_client(self):
        api_key = Config.AZURE_OPENAI_API_KEY
        if not api_key:
            raise ValueError("AZURE_OPENAI_API_KEY not configured")
        import openai
        return openai.AsyncAzureOpenAI(
            api_key=api_key,
            api_version="2024-02-01",
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
        )

    @with_content_safety(config=GUARDRAILS_CONFIG)
    async def generate_response_with_tools(
        self,
        prompt: str,
        tool_registry: ToolRegistry,
        context: Optional[str] = None,
        few_shot_examples: Optional[List[str]] = None,
        max_tool_rounds: int = 5,
    ) -> Dict[str, Any]:
        """
        LLM function-calling loop with tool integration.
        """
        client = self.get_llm_client()
        system_message = SYSTEM_PROMPT + "\n\nOutput Format: " + OUTPUT_FORMAT
        messages = [{"role": "system", "content": system_message}]
        if context:
            messages.append({"role": "system", "content": f"Context:\n{context}"})
        if few_shot_examples:
            for ex in few_shot_examples:
                messages.append({"role": "user", "content": ex})
        messages.append({"role": "user", "content": prompt})

        tools = tool_registry.to_openai_tools()
        tools_invoked = []

        for _round in range(max_tool_rounds):
            _t0 = _time.time()
            try:
                response = await client.chat.completions.create(
                    model=Config.LLM_MODEL or "gpt-4.1",
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    **Config.get_llm_kwargs(),
                )
                message = response.choices[0].message
                content = message.content
                try:
                    trace_model_call(
                        provider="azure",
                        model_name=Config.LLM_MODEL or "gpt-4.1",
                        prompt_tokens=getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0,
                        latency_ms=int((_time.time() - _t0) * 1000),
                        response_summary=content[:200] if content else "",
                    )
                except Exception:
                    pass
            except Exception as e:
                return {
                    "content": FALLBACK_RESPONSE,
                    "error": f"LLM API error: {str(e)}",
                    "tools_invoked": tools_invoked
                }

            if not getattr(message, "tool_calls", None):
                return {
                    "content": content,
                    "tools_invoked": tools_invoked
                }

            # Process each tool call
            messages.append(message)
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = tool_call.function.arguments
                tools_invoked.append(fn_name)
                _t0_tool = _time.time()
                try:
                    result = await tool_registry.execute_tool(fn_name, fn_args)
                    trace_tool_call(
                        tool_name=fn_name,
                        latency_ms=int((_time.time() - _t0_tool) * 1000),
                        args=fn_args,
                        output=str(result)[:200] if result else None,
                        status="success" if not result.get("error") else "error"
                    )
                except Exception as e:
                    trace_tool_call(
                        tool_name=fn_name,
                        latency_ms=int((_time.time() - _t0_tool) * 1000),
                        args=fn_args,
                        status="error",
                        error=e
                    )
                    result = {"error": f"Tool execution failed: {str(e)}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, default=str),
                })
        return {
            "content": FALLBACK_RESPONSE,
            "tools_invoked": tools_invoked
        }

# =========================
# Agent Orchestrator
# =========================
class AgentOrchestrator:
    def __init__(self):
        self.llm_service = LLMService()
        self.tool_registry = build_tool_registry()

    @with_content_safety(config=GUARDRAILS_CONFIG)
    async def process_user_query(self, user_input: str, attachments: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Main entry point for user queries.
        """
        async with trace_step(
            "process_user_query",
            step_type="llm_call",
            decision_summary="Process user query and orchestrate LLM + tool calls",
            output_fn=lambda r: f"success={r.get('success', False)}"
        ) as step:
            context = None
            if attachments:
                context = f"Attachments: {attachments}"
            result = await self.llm_service.generate_response_with_tools(
                prompt=user_input,
                tool_registry=self.tool_registry,
                context=context,
                few_shot_examples=[
                    "Process new RFQ from customer email with attached product list.",
                    "Generate quote for revision of previous RFQ."
                ]
            )
            content = sanitize_llm_output(result.get("content", ""), content_type="text")
            return {
                "success": True if content else False,
                "content": content if content else FALLBACK_RESPONSE,
                "tool_calls_made": result.get("tools_invoked", []),
                "error": None if content else result.get("error", None)
            }

# =========================
# Main Agent Class
# =========================
class SalesQuoteBuilderAgent:
    def __init__(self):
        self.orchestrator = AgentOrchestrator()

    @trace_agent(agent_name=_obs_settings.AGENT_NAME, project_name=_obs_settings.PROJECT_NAME)
    @with_content_safety(config=GUARDRAILS_CONFIG)
    async def process(self, query: str, attachments: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Main agent entrypoint.
        """
        async with trace_step(
            "agent_process",
            step_type="process",
            decision_summary="Agent process entrypoint",
            output_fn=lambda r: f"success={r.get('success', False)}"
        ) as step:
            return await self.orchestrator.process_user_query(query, attachments)

# =========================
# FastAPI App & Observability Lifespan
# =========================
@asynccontextmanager
async def _obs_lifespan(application):
    """Initialise observability on startup, clean up on shutdown."""
    try:
        _obs_startup_logger.info('')
        _obs_startup_logger.info('========== Agent Configuration Summary ==========')
        _obs_startup_logger.info(f'Environment: {getattr(Config, "ENVIRONMENT", "N/A")}')
        _obs_startup_logger.info(f'Agent: {getattr(Config, "AGENT_NAME", "N/A")}')
        _obs_startup_logger.info(f'Project: {getattr(Config, "PROJECT_NAME", "N/A")}')
        _obs_startup_logger.info(f'LLM Provider: {getattr(Config, "MODEL_PROVIDER", "N/A")}')
        _obs_startup_logger.info(f'LLM Model: {getattr(Config, "LLM_MODEL", "N/A")}')
        _cs_endpoint = getattr(Config, 'AZURE_CONTENT_SAFETY_ENDPOINT', None)
        _cs_key = getattr(Config, 'AZURE_CONTENT_SAFETY_KEY', None)
        if _cs_endpoint and _cs_key:
            _obs_startup_logger.info('Content Safety: Enabled (Azure Content Safety)')
            _obs_startup_logger.info(f'Content Safety Endpoint: {_cs_endpoint}')
        else:
            _obs_startup_logger.info('Content Safety: Not Configured')
        _obs_startup_logger.info('Observability Database: Azure SQL')
        _obs_startup_logger.info(f'Database Server: {getattr(Config, "OBS_AZURE_SQL_SERVER", "N/A")}')
        _obs_startup_logger.info(f'Database Name: {getattr(Config, "OBS_AZURE_SQL_DATABASE", "N/A")}')
        _obs_startup_logger.info('===============================================')
        _obs_startup_logger.info('')
    except Exception as _e:
        _obs_startup_logger.warning('Config summary failed: %s', _e)

    _obs_startup_logger.info('')
    _obs_startup_logger.info('========== Content Safety & Guardrails ==========')
    if GUARDRAILS_CONFIG.get('content_safety_enabled'):
        _obs_startup_logger.info('Content Safety: Enabled')
        _obs_startup_logger.info(f'  - Severity Threshold: {GUARDRAILS_CONFIG.get("content_safety_severity_threshold", "N/A")}')
        _obs_startup_logger.info(f'  - Check Toxicity: {GUARDRAILS_CONFIG.get("check_toxicity", False)}')
        _obs_startup_logger.info(f'  - Check Jailbreak: {GUARDRAILS_CONFIG.get("check_jailbreak", False)}')
        _obs_startup_logger.info(f'  - Check PII Input: {GUARDRAILS_CONFIG.get("check_pii_input", False)}')
        _obs_startup_logger.info(f'  - Check Credentials Output: {GUARDRAILS_CONFIG.get("check_credentials_output", False)}')
    else:
        _obs_startup_logger.info('Content Safety: Disabled')
    _obs_startup_logger.info('===============================================')
    _obs_startup_logger.info('')

    _obs_startup_logger.info('========== Initializing Agent Services ==========')
    # 1. Observability DB schema (imports are inside function — only needed at startup)
    try:
        from observability.database.engine import create_obs_database_engine
        from observability.database.base import ObsBase
        import observability.database.models  # noqa: F401
        _obs_engine = create_obs_database_engine()
        ObsBase.metadata.create_all(bind=_obs_engine, checkfirst=True)
        _obs_startup_logger.info('✓ Observability database connected')
    except Exception as _e:
        _obs_startup_logger.warning('✗ Observability database connection failed (metrics will not be saved)')
    # 2. OpenTelemetry tracer (initialize_tracer is pre-injected at top level)
    try:
        _t = initialize_tracer()
        if _t is not None:
            _obs_startup_logger.info('✓ Telemetry monitoring enabled')
        else:
            _obs_startup_logger.warning('✗ Telemetry monitoring disabled')
    except Exception as _e:
        _obs_startup_logger.warning('✗ Telemetry monitoring failed to initialize')
    _obs_startup_logger.info('=================================================')
    _obs_startup_logger.info('')
    yield

app = FastAPI(
    title="Emerson Sales Quote Builder Agent",
    description="Automates the creation, validation, approval, and delivery of sales quotations in response to customer RFQs received via email.",
    version=Config.SERVICE_VERSION if hasattr(Config, "SERVICE_VERSION") else "1.0.0",
    lifespan=_obs_lifespan
)

# =========================
# Error Handling
# =========================
@app.exception_handler(ValidationError)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def validation_exception_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Input validation error",
            "details": exc.errors(),
            "tips": [
                "Ensure your JSON is well-formed.",
                "Check for missing required fields.",
                "Remove trailing commas and fix quotes.",
                "Limit text fields to 50,000 characters."
            ]
        }
    )

@app.exception_handler(json.decoder.JSONDecodeError)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def json_decode_exception_handler(request: Request, exc: json.decoder.JSONDecodeError):
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": "Malformed JSON request",
            "details": str(exc),
            "tips": [
                "Ensure your JSON is well-formed.",
                "Check for missing or extra commas.",
                "Use double quotes for keys and string values.",
                "Limit text fields to 50,000 characters."
            ]
        }
    )

@app.exception_handler(Exception)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def generic_exception_handler(request: Request, exc: Exception):
    logging.getLogger(__name__).error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "details": str(exc),
            "tips": [
                "Try again later.",
                "If the problem persists, contact support."
            ]
        }
    )

# =========================
# Health Check Endpoint
# =========================
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}

# =========================
# Main Query Endpoint
# =========================
agent_instance = SalesQuoteBuilderAgent()

@app.post("/query", response_model=QueryResponse)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def query_endpoint(req: QueryRequest):
    """
    Main endpoint for user queries.
    """
    async with trace_step(
        "api_query",
        step_type="process",
        decision_summary="API query endpoint",
        output_fn=lambda r: f"success={r.get('success', False)}"
    ) as step:
        try:
            result = await agent_instance.process(query=req.query, attachments=req.attachments)
            return QueryResponse(
                success=result.get("success", False),
                content=result.get("content"),
                error=result.get("error"),
                tool_calls_made=result.get("tool_calls_made")
            )
        except Exception as e:
            logging.getLogger(__name__).error("Agent processing error: %s", e, exc_info=True)
            return QueryResponse(
                success=False,
                content=None,
                error=str(e),
                tool_calls_made=None
            )

# =========================
# Entrypoint
# =========================
async def _run_agent():
    """Entrypoint: runs the agent with observability (trace collection only)."""
    import uvicorn

    # Unified logging config — routes uvicorn, agent, and observability through
    # the same handler so all telemetry appears in a single consistent stream.
    _LOG_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(name)s: %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "agent":          {"handlers": ["default"], "level": "INFO", "propagate": False},
            "__main__":       {"handlers": ["default"], "level": "INFO", "propagate": False},
            "observability": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "config": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "azure":   {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "urllib3": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
    }

    config = uvicorn.Config(
        "agent:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
        log_config=_LOG_CONFIG,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    _asyncio.run(_run_agent())