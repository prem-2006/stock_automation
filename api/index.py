"""
Vercel Serverless Entry Point.

This file exposes the FastAPI app as a Vercel serverless function.
Vercel routes all requests to this handler via vercel.json.
"""

from app.main import app

# Vercel expects a variable named 'app' or a handler
# FastAPI's ASGI app works directly with Vercel's Python runtime
