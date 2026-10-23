# Context: repository source plus dist/*.whl and an executable dist/uv for Linux.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY dist/uv /usr/local/bin/uv
COPY dist/*.whl /tmp/wheels/
RUN useradd --create-home console
USER console
RUN uv python install 3.12 && uv tool install /tmp/wheels/*.whl
ENV PATH="/home/console/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COPY --chown=console:console tests /home/console/acceptance/tests
WORKDIR /home/console/acceptance
# Root permits native namespaces on hosts that restrict unprivileged setup.
# Permission-sensitive direct cases must still run without root bypass.
USER root
RUN uv python install 3.12 && uv tool install /tmp/wheels/*.whl
ENV PATH="/root/.local/bin:/home/console/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CMD ["sh", "-c", "runuser -u console -- uv run --no-project --python 3.12 tests/without_r.py --execution direct && uv run --no-project --python 3.12 tests/without_r.py --execution sandbox"]
