"""
FastAPI + SSE web interface for the Autonomous Data Analyst.

Endpoints
  GET  /                 Serve the web UI (static/index.html)
  POST /analyze          Stream analysis events as Server-Sent Events
  GET  /charts/{file}    Serve generated chart images
  GET  /health           Health check
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth.dependencies import RequireAnalyst
from orchestration import run_analysis

load_dotenv()

CHARTS_DIR = Path(os.getenv("CHARTS_DIR", "output/charts"))
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Autonomous Data Analyst")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/analyze")
async def analyze(request: AnalyzeRequest, user: RequireAnalyst):
    """
    Stream analysis events as Server-Sent Events.

    Each event line is:  data: <json>\n\n
    Event types: agent_switch, text_delta, tool_call, tool_result, chart, done, error
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    async def generate():
        try:
            async for event in run_analysis(request.question, workspace_id=user.workspace_id):
                # Rewrite filesystem chart path → servable URL
                if event.get("type") == "chart":
                    filename = Path(event["path"]).name
                    event = {**event, "url": f"/charts/{filename}"}
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.error("Streaming error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",        # prevent nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/charts/{filename}")
async def serve_chart(filename: str):
    """Serve a generated chart PNG.  No path traversal allowed."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    chart_path = CHARTS_DIR / filename
    if not chart_path.exists():
        raise HTTPException(status_code=404, detail="Chart not found.")

    return FileResponse(str(chart_path), media_type="image/png")


@app.get("/health")
async def health():
    db_path = Path(os.getenv("DB_PATH", "data/sample.db"))
    return {"status": "ok", "db_exists": db_path.exists()}


# ---------------------------------------------------------------------------
# Dev entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8000))
    logger.info("Starting server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
