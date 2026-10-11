FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLASK_APP=wsgi
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 5000
ENTRYPOINT ["./docker-entrypoint.sh"]
# One worker on purpose: rate limits are kept in process memory
# (RATELIMIT_STORAGE_URI=memory://), so extra workers would each enforce
# their own separate limit. Threads give concurrency instead.
CMD ["gunicorn", "wsgi:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--access-logfile", "-"]
