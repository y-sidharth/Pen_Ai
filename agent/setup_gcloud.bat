@echo off
REM setup_gcloud.bat - one-click setup for Gemini + Firestore + Storage
REM Run this from agent\ folder or project root.
echo === Pen AI - Google Cloud Setup (Gemini 3.5+ / ADK / Firestore / Storage) ===
echo.

where gcloud >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] gcloud CLI not found. Install from https://cloud.google.com/sdk/docs/install
  pause
  exit /b 1
)

set /p PROJECT_ID=Enter GCP Project ID (e.g. pen-ai-hack): 
if "%PROJECT_ID%"=="" set PROJECT_ID=pen-ai-hack

echo.
echo Creating / selecting project %PROJECT_ID% ...
gcloud projects create %PROJECT_ID% --name="Pen AI" 2>nul
gcloud config set project %PROJECT_ID%

echo.
echo Enabling APIs (Vertex AI, Firestore, Storage)...
gcloud services enable aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com run.googleapis.com

echo.
echo Authenticating (browser will open)...
gcloud auth application-default login

echo.
echo Installing Python deps...
if exist python-portable\python.exe (
  python-portable\python.exe -m pip install -r requirements.txt
) else (
  pip install -r requirements.txt
)

echo.
echo --- Create Firestore (if not exists) ---
echo Run: gcloud firestore databases create --location=us-central1  (choose Firestore Native)
echo Or create manually in console: https://console.cloud.google.com/firestore

echo.
echo --- Create GCS bucket ---
set /p BUCKET=Enter GCS bucket name (empty to skip, e.g. pen-ai-brain-xxxxx): 
if not "%BUCKET%"=="" (
  gsutil mb -l us-central1 gs://%BUCKET% 2>nul
  echo Bucket gs://%BUCKET% ready.
  echo Add to agent\.env: GCS_BUCKET=%BUCKET%
)

echo.
echo --- Create agent\.env if missing ---
if not exist .env (
  if exist .env.example copy .env.example .env
  echo Edit .env and set GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT=%PROJECT_ID%
  notepad .env
)

echo.
echo Done. Test with:
echo   python gemini_client.py
echo   python -c "from adk_agent.agent import health; import json; print(json.dumps(health(), indent=2))"
echo   python validate_package.py
pause
