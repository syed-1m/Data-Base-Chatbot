"""
app/api/v1/stream_query.py
===========================
FastAPI router for the streaming Query Execution Engine.

Endpoint
--------
POST /api/v1/chat/query

Returns a ``text/event-stream`` ``StreamingResponse``.  Each SSE frame is a
JSON object with shape::

    data: {"stage": "<stage>", "elapsed_ms": <float>, "data": { ... }}\n\n

Client consumption
------------------
  Browser (EventSource API)::

      const es = new EventSource('/api/v1/chat/query')   // must use fetch for POST
      // Since EventSource doesn't support POST, use fetch with ReadableStream:
      const resp = await fetch('/api/v1/chat/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
          body: JSON.stringify({ connection_id: '...', message: '...' })
      })
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const text = decoder.decode(value)
          // Each line starting with "data: " is one event frame
          const lines = text.split('\n').filter(l => l.startsWith('data: '))
          for (const line of lines) {
              const event = JSON.parse(line.slice(6))
              console.log(event.stage, event.data)
          }
      }

  Python httpx (async)::

      async with httpx.AsyncClient() as client:
          async with client.stream('POST', '/api/v1/chat/query', json={...}) as r:
              async for line in r.aiter_lines():
                  if line.startswith('data: '):
                      event = json.loads(line[6:])

Design decisions
----------------
* **``StreamingResponse``** with ``media_type="text/event-stream"`` – standard
  SSE; no websocket dependency required.
* **``background=None``** – we do NOT use FastAPI ``BackgroundTasks`` here
  because the generator IS the response; it runs inline during streaming.
* **Request body via JSON** – SSE normally uses GET, but our endpoint needs a
  JSON body for the NL query.  We use ``POST`` with ``StreamingResponse``.
* **``X-Accel-Buffering: no``** header – tells NGINX (if used as reverse proxy)
  not to buffer the SSE stream.
* **Dependency injection** – ``get_db_session`` provides the app DB session;
  the connection to the *user's* database is looked up via ``connection_manager``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.stream import StreamQueryRequest
from app.services.stream_service import run_query_pipeline

router = APIRouter(prefix="/chat", tags=["Query Execution Engine"])


@router.post(
    "/query",
    summary="Execute NL-to-SQL query with live progress streaming",
    description=(
        "Accepts a natural-language question and an active connection ID. "
        "Streams live pipeline progress updates via Server-Sent Events (SSE). "
        "Each event frame is a JSON object with `stage`, `elapsed_ms`, and `data` fields. "
        "The final `complete` frame contains the full query results."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "SSE stream of pipeline events",
            "content": {
                "text/event-stream": {
                    "example": (
                        'data: {"stage":"received","elapsed_ms":0.1,"data":{...}}\n\n'
                        'data: {"stage":"complete","elapsed_ms":843.2,"data":{...}}\n\n'
                    )
                }
            },
        }
    },
)
async def stream_query(
    request: StreamQueryRequest,
    db: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """
    POST /api/v1/chat/query

    Stream the NL-to-SQL pipeline result via SSE.

    Body fields:
    - **connection_id**: UUID from ``POST /api/v1/database/connect``
    - **message**: Natural-language question (1–4000 chars)
    - **session_id** *(optional)*: Persist conversation to a chat session
    - **timeout_seconds** *(optional)*: Execution timeout, default 30s
    - **max_rows** *(optional)*: Maximum result rows, default 500
    """
    generator = run_query_pipeline(request=request, app_db=db)

    return StreamingResponse(
        content=generator,
        media_type="text/event-stream",
        headers={
            # Disable NGINX buffering so events reach the client immediately
            "X-Accel-Buffering": "no",
            # Keep the connection alive; client should not cache
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
