FROM python:3.11-slim

RUN pip install --no-cache-dir matplotlib==3.8.4 pandas==2.2.2 numpy==1.26.4

RUN useradd -m -u 1000 sandbox

ENV MPLCONFIGDIR=/tmp

USER sandbox
WORKDIR /app
COPY runner.py .

ENTRYPOINT ["python", "runner.py"]
