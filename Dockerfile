FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /build
COPY deploy_payload ./deploy_payload
RUN python -c "import base64,pathlib,zipfile; p=pathlib.Path('/build/deploy_payload'); data=''.join(x.read_text() for x in sorted(p.glob('part*.b64'))); z=pathlib.Path('/tmp/agent.zip'); z.write_bytes(base64.b64decode(data)); zipfile.ZipFile(z).extractall('/opt/nr7')"

WORKDIR /opt/nr7/NR7_Cloud_Demo_Agent
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "cloud_app.py"]
