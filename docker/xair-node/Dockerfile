# Node image for the distributed testbed (docker/distributed): XAIR, gateway,
# and benchmark nodes run from this image with the repository mounted at /repo.
FROM python:3.12-slim
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock
ENV PYTHONPATH=/repo PYTHONUNBUFFERED=1
WORKDIR /repo
