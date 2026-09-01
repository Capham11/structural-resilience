#!/bin/bash
osascript -e 'tell application "Terminal"
    do script "cd /Users/chris/Desktop/StructuralResilience/backend && uvicorn main:app --reload --port 8000"
    do script "cd /Users/chris/Desktop/StructuralResilience/frontend && npm start"
end tell'
