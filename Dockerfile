FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /build
COPY deploy_payload ./deploy_payload
COPY deploy_overrides ./deploy_overrides
RUN python -c "import base64,pathlib,zipfile; p=pathlib.Path('/build/deploy_payload'); data=''.join(x.read_text() for x in sorted(p.glob('part*.b64'))); z=pathlib.Path('/tmp/agent.zip'); z.write_bytes(base64.b64decode(data)); zipfile.ZipFile(z).extractall('/opt/nr7')"
RUN python -c "import base64,pathlib; p=pathlib.Path('/build/deploy_overrides'); data=''.join(x.read_text() for x in sorted(p.glob('fixed_overlay_part*.b64'))); pathlib.Path('/tmp/restore_settings.py').write_bytes(base64.b64decode(data))"
RUN python /tmp/restore_settings.py /opt/nr7/NR7_Cloud_Demo_Agent

WORKDIR /opt/nr7/NR7_Cloud_Demo_Agent
RUN python -m py_compile cloud_app.py demo_trader.py strategy.py
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "cloud_app.py"]
