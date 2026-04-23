# Procfile — tells Render (and Heroku) how to start your app
#
# Render reads this automatically when you deploy.
# $PORT is set by Render at runtime — do not hardcode a port number.
#
# --workers 1  → single worker (fine for free tier; bump to 2-4 on paid plans)
# --timeout 60 → 60-second request timeout (increase if scrapers are slow)

web: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --timeout-keep-alive 60
