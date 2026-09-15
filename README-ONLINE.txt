SPQR JEOPARDY - ONLINE HOSTING

This version is prepared for a public Python web host such as Render.

Render:
1. Put index.html, server.py, render.yaml, and .python-version in your GitHub repo.
2. On Render choose New -> Web Service and connect the repo.
3. Use the Python runtime.
4. Start command: python server.py
5. The app uses the PORT environment variable and listens on 0.0.0.0.
6. After deployment, open the HTTPS onrender.com URL.
7. Send that URL to your friends. They only need a browser.

The game server stores the live game state in memory, so keep one deployed server
running for everyone in the same game.
