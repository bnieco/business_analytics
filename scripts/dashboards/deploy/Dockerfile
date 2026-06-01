FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY phase1_page.py .

EXPOSE 8788
CMD ["python", "phase1_page.py", "--serve"]
