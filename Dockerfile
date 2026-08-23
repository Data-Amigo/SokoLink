# Biashara Mall — production image.
#
# WHY A DOCKERFILE RATHER THAN LETTING THE BUILDER GUESS. Three builds failed
# with `pip: command not found` because the builder never detected Python: the
# application lives in backend/, and language detection looks at the repository
# root. Root marker files were added and the builder was switched, and the same
# command kept running — because a Build Command saved in the Railway dashboard
# overrides the repository, and no push can clear it.
#
# This removes the guessing entirely. The image says which Python it wants,
# installs what it needs, and starts what it starts. Nothing is inferred, so
# nothing can be inferred wrongly.

FROM python:3.11-slim

# Faster, quieter, and no .pyc files baked into a layer that will be thrown away.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, and only the requirements file, so that Docker's layer
# cache survives ordinary code changes. Copying the whole tree here would
# reinstall every package on every commit.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY . .

WORKDIR /app/backend

# Migrations run before the server, on every boot. Alembic is a no-op at head,
# so it is safe to repeat, and it means a deploy can never serve a schema the
# code does not expect.
#
# WHY `sh -c` AND WHY `exec`. A shell is needed because $PORT is injected at
# runtime and the plain exec form would pass it as a literal string. But a bare
# shell-form CMD leaves /bin/sh as PID 1, and it does NOT forward SIGTERM to
# uvicorn — so Railway's shutdown signal would be swallowed and the container
# killed after the grace period instead of draining. `exec` replaces the shell
# with uvicorn, which then receives signals directly and shuts down cleanly.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
