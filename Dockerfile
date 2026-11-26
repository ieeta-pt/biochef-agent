# The agent, as a container.
#
# Debian rather than alpine: snakemake shells out for every rule and the tools
# it runs are ordinary glibc binaries pulled from the registry, so a musl base
# would be a trap that only springs once a real tool is used.
FROM python:3.13-slim-bookworm

# Pinned requirements only, and in their own layer, so an edit to the source
# does not reinstall ninety packages. The build should fail if a pin no longer
# resolves rather than quietly installing something adjacent.
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# Not root. The agent executes tool binaries pulled from a registry, and running
# them as uid 0 would leave the container boundary as the only thing between an
# untrusted binary and everything the container can reach.
#
# /app has to be writable by that user: this version of the agent writes uploads
# to ./tmp and pulls each tool bundle into a directory named after it, both
# relative to the working directory.
RUN useradd --create-home --uid 10001 agent \
 && chown -R agent:agent /app
USER agent

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# uvicorn directly rather than run.sh, which exists to build a virtualenv on a
# developer's machine. The image already has the dependencies.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
