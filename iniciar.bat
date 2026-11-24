@echo off
REM Inicia a aplicacao Juntar Guias (http://<servidor>:8000)
REM Pode ser usado no Agendador de Tarefas do Windows ("Ao iniciar o computador").
cd /d "%~dp0"
".venv\Scripts\python.exe" app.py
