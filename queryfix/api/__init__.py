"""FastAPI surface.

:mod:`.main` holds the app. Route modules (``routes/webhooks.py`` for the GitHub
webhook receiver, ``routes/runs.py`` for manual replay) and ``deps.py`` land here
as those endpoints are implemented, rather than as empty packages now.
"""
