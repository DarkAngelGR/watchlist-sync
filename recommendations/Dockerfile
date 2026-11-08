FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py /app/app.py
RUN useradd --system --uid 10001 --home /app recommender \
    && mkdir /data && chown recommender:recommender /data
USER recommender
EXPOSE 8788
CMD ["python", "/app/app.py"]
