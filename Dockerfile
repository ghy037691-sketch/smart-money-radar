FROM apify/actor-python:3.12

# No third-party runtime dependencies: deterministic, small attack surface, no paid keys.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY . /usr/src/app
WORKDIR /usr/src/app

CMD ["python3", "main.py"]
