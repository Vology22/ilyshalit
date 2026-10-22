"""Точка входа для uvicorn: uvicorn chzkiz.web.main:app"""
from .app import create_app

app = create_app()
