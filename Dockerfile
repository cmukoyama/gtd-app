FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/gtd.db PORT=8000
RUN useradd -m app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} app:app"]
