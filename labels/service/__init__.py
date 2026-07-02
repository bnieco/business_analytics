"""Label Printing Service — the HTTP front door over `labels.engine`.

Run locally:
  cd ~/PycharmProjects/business_analytics
  ./venv/bin/python -m uvicorn labels.service.app:app --host 127.0.0.1 --port 8744

Set LABELS_DRY_RUN=1 to assemble + log without ever opening a printer socket.
"""
