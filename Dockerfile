# WAKE: the one-command demo and the trials, on any machine that runs Docker.
#   docker build -t wake .
#   docker run --rm -it --init -p 127.0.0.1:7700-7701:7700-7701 wake demo     # the demo and the live view
#   docker run --rm -it --init wake trials                                     # tests and trials
#   docker run --rm -it --init wake shell                                      # a shell as the person, with `wake`
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git procps sqlite3 rsync curl ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Relate's command line, for live following (`relate serve --read-only=live`). The installer checks
# the release signature with ssh-keygen and fails closed.
ENV DO_NOT_TRACK=1
RUN curl -sSfLo /tmp/install-relate.sh https://relatedb.ai/install.sh \
 && HOME=/opt/relate bash /tmp/install-relate.sh \
 && ln -s /opt/relate/.local/bin/relate /usr/local/bin/relate \
 && rm /tmp/install-relate.sh

WORKDIR /opt/wake
COPY pyproject.toml README.md ./
COPY wake ./wake
RUN pip install --no-cache-dir .
COPY . .

RUN useradd --create-home --uid 10001 person
EXPOSE 7700 7701
ENTRYPOINT ["/opt/wake/demo/entrypoint.sh"]
CMD ["demo"]
