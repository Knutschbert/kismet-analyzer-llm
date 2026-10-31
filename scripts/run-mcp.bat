@echo off
setlocal

if not defined KISMET_LLM_DATA set "KISMET_LLM_DATA=%~dp0..\output\generated\tree\TBL"

echo Starting kismet-analyzer MCP server with catalog:
echo   "%KISMET_LLM_DATA%"
python -m mcp_server.server
