@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m agent_harness.interfaces.cli chat --config examples\ollama.toml --user local-user %*
