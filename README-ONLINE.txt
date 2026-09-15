SPQR JEOPARDY - REAL-TIME ONLINE VERSION

This version uses WebSockets for multiplayer updates and cursor movement.

Files to upload to GitHub:
- index.html
- server.py
- requirements.txt
- render.yaml

Render:
1. Push/commit these files to your GitHub repo.
2. In Render, redeploy the existing Web Service from the repo.
3. The service should use: pip install -r requirements.txt
4. Start command: python server.py
5. Health check: /api/health

IMPORTANT: Render Free web services sleep after 15 minutes without inbound traffic.
The first person connecting after sleep can see a Render loading page while the
service wakes up. WebSockets make the multiplayer connection much smoother once
it is running.
