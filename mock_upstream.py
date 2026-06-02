# mock_upstream.py
# A simple FastAPI mock upstream for local development and demo.
# Run with: uvicorn mock_upstream:app --port 9000
#
# This is NOT part of Aegis — it is a stand-in upstream service for testing.
# In production, Aegis proxies to your real service.

from fastapi import FastAPI, Request

app = FastAPI(title="Mock Upstream")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def catch_all(request: Request):
    """Accept any request and return a clean JSON response."""
    return {
        "status": "ok",
        "message": "payment accepted",
        "upstream": "mock",
        "path": request.url.path,
    }
