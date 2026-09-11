FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY sql ./sql
# The layer predicates on every read of contact_weekly_assignment are pinned by tests, and the
# image does not build if one of them is gone (2026-09-11). Standard library only.
COPY tests ./tests
RUN python -m unittest discover -s tests
ENTRYPOINT ["python", "main.py"]
