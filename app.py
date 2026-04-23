"""
FastAPI + SSE web interface for the Autonomous Data Analyst.

Endpoints
  GET  /                       Serve the web UI (static/index.html)
  POST /analyze                Stream analysis events as Server-Sent Events
  GET  /charts/{file}          Serve generated chart images
  GET  /health                 Health check
  POST /datasources            Create a datasource record
  GET  /datasources            List datasources for the current workspace
  DELETE /datasources/{id}     Delete a datasource record
"""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from auth.dependencies import RequireAnalyst
from db.models import DataSource
from db.session import get_db_session
from orchestration import run_analysis

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
    datasource_id: str | None = None


class DataSourceCreate(BaseModel):
    name: str
    type: Literal["sqlite", "postgres", "bigquery", "snowflake"]
    connection_secret_ref: str
    default_schema: str | None = None
    row_limit: int = 50


class DataSourceResponse(BaseModel):
    id: str
    name: str
    type: str
    connection_secret_ref: str
    default_schema: str | None
    row_limit: int
    created_at: datetime


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
            async for event in run_analysis(
                request.question,
                workspace_id=user.workspace_id,
                datasource_id=request.datasource_id,
            ):
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


@app.post("/datasources", status_code=201, response_model=DataSourceResponse)
async def create_datasource(body: DataSourceCreate, user: RequireAnalyst):
    """Create a new datasource record for the current workspace."""
    async with get_db_session(workspace_id=user.workspace_id) as session:
        ds = DataSource(
            id=uuid.uuid4(),
            workspace_id=uuid.UUID(user.workspace_id),
            name=body.name,
            type=body.type,
            connection_secret_ref=body.connection_secret_ref,
            default_schema=body.default_schema,
            row_limit=body.row_limit,
        )
        session.add(ds)
        await session.commit()
        await session.refresh(ds)

    return DataSourceResponse(
        id=str(ds.id),
        name=ds.name,
        type=ds.type,
        connection_secret_ref=ds.connection_secret_ref,
        default_schema=ds.default_schema,
        row_limit=ds.row_limit,
        created_at=ds.created_at,
    )


@app.get("/datasources", response_model=list[DataSourceResponse])
async def list_datasources(user: RequireAnalyst):
    """List all datasources for the current workspace."""
    async with get_db_session(workspace_id=user.workspace_id) as session:
        result = await session.execute(
            select(DataSource).where(
                DataSource.workspace_id == uuid.UUID(user.workspace_id)
            )
        )
        sources = result.scalars().all()

    return [
        DataSourceResponse(
            id=str(ds.id),
            name=ds.name,
            type=ds.type,
            connection_secret_ref=ds.connection_secret_ref,
            default_schema=ds.default_schema,
            row_limit=ds.row_limit,
            created_at=ds.created_at,
        )
        for ds in sources
    ]


@app.delete("/datasources/{datasource_id}", status_code=204)
async def delete_datasource(datasource_id: str, user: RequireAnalyst):
    """Delete a datasource record. Only the owning workspace may delete it."""
    try:
        ds_uuid = uuid.UUID(datasource_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datasource ID.")

    async with get_db_session(workspace_id=user.workspace_id) as session:
        result = await session.execute(
            select(DataSource).where(
                DataSource.id == ds_uuid,
                DataSource.workspace_id == uuid.UUID(user.workspace_id),
            )
        )
        ds = result.scalar_one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Datasource not found.")
        await session.delete(ds)
        await session.commit()


@app.get("/me")
async def get_me(user: RequireAnalyst):
    """Return the calling user's identity and workspace context."""
    is_dev = os.getenv("DEV_AUTH_BYPASS", "").lower() in ("1", "true", "yes")
    workspace_name = "Dev Workspace" if is_dev else f"Workspace {user.workspace_id[:8]}"
    return {
        "sub": user.sub,
        "workspace_id": user.workspace_id,
        "workspace_name": workspace_name,
        "role": user.role,
        "is_dev": is_dev,
    }


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
